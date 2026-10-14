/* 无损剪辑 — 前端逻辑（无构建步骤，纯浏览器 ES2020） */
(function () {
  'use strict';

  var PREFIX = (window.VC_PREFIX || '').replace(/\/$/, '');
  var $ = function (sel) { return document.querySelector(sel); };

  var state = {
    ctx: null,
    cur: null,          // 当前载入时间轴的文件探测结果
    clips: [],          // {path,name,start,end,duration,ext}
    active: -1,         // 片段列表中正在编辑的下标
    keyframes: [],
    duration: 0,
    fps: 0,
    inT: 0,
    outT: 0,
    snap: true,
    jobs: [],
    timer: null,
    pfTimer: null,
    drag: null,
    picker: { mode: 'file', path: '', parent: '', onPick: null, onlyDirs: false }
  };

  /* ------------------------------------------------------------------ */
  /* 基础工具                                                             */
  /* ------------------------------------------------------------------ */

  function api(path, opts) {
    opts = opts || {};
    var init = { method: opts.method || 'GET', headers: {} };
    if (opts.body !== undefined) {
      init.headers['Content-Type'] = 'application/json';
      init.body = JSON.stringify(opts.body);
    }
    return fetch(PREFIX + path, init).then(function (res) {
      return res.text().then(function (text) {
        var data = null;
        try { data = text ? JSON.parse(text) : null; } catch (e) { data = { error: text.slice(0, 300) }; }
        if (!res.ok) {
          var err = new Error((data && data.error) || ('请求失败 ' + res.status));
          err.status = res.status;
          throw err;
        }
        return data;
      });
    });
  }

  function streamUrl(file) { return PREFIX + '/api/stream?path=' + encodeURIComponent(file); }
  function thumbUrl(file, t, w) {
    return PREFIX + '/api/thumb?path=' + encodeURIComponent(file) + '&t=' + t + '&w=' + (w || 160);
  }
  function downloadUrl(file) { return PREFIX + '/api/download?path=' + encodeURIComponent(file); }

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  function fmtTime(sec, short) {
    sec = Math.max(0, Number(sec) || 0);
    var h = Math.floor(sec / 3600);
    var m = Math.floor((sec % 3600) / 60);
    var s = sec % 60;
    var ss = (s < 10 ? '0' : '') + s.toFixed(3);
    if (short && h === 0) return m + ':' + ss;
    return (h < 10 ? '0' : '') + h + ':' + (m < 10 ? '0' : '') + m + ':' + ss;
  }

  function fmtDur(sec) {
    sec = Number(sec) || 0;
    if (sec < 60) return sec.toFixed(2) + ' 秒';
    if (sec < 3600) return Math.floor(sec / 60) + ' 分 ' + (sec % 60).toFixed(1) + ' 秒';
    return Math.floor(sec / 3600) + ' 时 ' + Math.floor((sec % 3600) / 60) + ' 分';
  }

  function fmtSize(n) {
    n = Number(n) || 0;
    if (n < 1024) return n + ' B';
    if (n < 1048576) return (n / 1024).toFixed(1) + ' KB';
    if (n < 1073741824) return (n / 1048576).toFixed(1) + ' MB';
    return (n / 1073741824).toFixed(2) + ' GB';
  }

  /* ------------------------------------------------------------------ */
  /* 逐帧定位                                                             */
  /* ------------------------------------------------------------------ */

  // ffprobe 的帧率可能是数字（25）或分数（30000/1001），两种都要能解析
  function fpsOf(video) {
    if (!video) return 0;
    var raw = video.fps;
    if (raw === undefined || raw === null || raw === '') return 0;
    var m = String(raw).match(/^\s*(\d+(?:\.\d+)?)\s*\/\s*(\d+(?:\.\d+)?)\s*$/);
    var v;
    if (m) {
      var den = Number(m[2]);
      v = den ? Number(m[1]) / den : 0;
    } else {
      v = parseFloat(raw);
    }
    return (isFinite(v) && v > 0 && v < 1000) ? v : 0;
  }

  function fmtFps(f) {
    return (Math.round(f * 100) / 100) + ' fps';
  }

  function fmtFrame(t) {
    if (!state.cur) return '—';
    var label = fmtTime(t, true);
    if (state.fps > 0) label += ' · 帧 ' + Math.round(t * state.fps);
    return label;
  }

  function syncNow(t) {
    var d = state.duration || 0;
    if (d) $('#caret').style.left = (Math.min(t, d) / d * 100) + '%';
    var el = $('#nowBadge');
    if (el) el.textContent = fmtFrame(t);
  }

  function stepFrame(dir, seconds) {
    var player = $('#player');
    if (!state.cur || !player.src) return;
    var d = state.duration || (isFinite(player.duration) ? player.duration : 0);
    var cur = player.currentTime || 0;
    var f = state.fps > 0 ? state.fps : 25;
    var t;
    if (seconds) {
      t = cur + dir * seconds;                 // 秒级步进（Shift + 方向键）
    } else {
      // 按帧号计算目标时间，而不是简单加 1/f：后者会因浮点误差停在原帧，
      // 而且来回步进会累积漂移。这里每次都用当前时间重新求帧号。
      var idx = Math.round(cur * f);
      t = (idx + dir) / f;
      // 目标帧内偏一点，避免请求时间正好落在帧边界上被吸附回原帧
      t += (dir > 0 ? 0.4 : 0.001) / f;
    }
    if (t < 0) t = 0;
    if (d && t > d) t = d;
    try { player.pause(); } catch (e) { /* 未加载时部分浏览器会抛错 */ }
    try { player.currentTime = t; } catch (e) { return; }

    // 浏览器会把 currentTime 吸附到最近可解码的帧，因此以回读值为准
    var apply = function () { syncNow(player.currentTime); };
    apply();
    if (player.seeking) {
      var once = function () { player.removeEventListener('seeked', once); apply(); };
      player.addEventListener('seeked', once);
    } else {
      setTimeout(apply, 0);
    }
  }

  var toastTimer = null;
  function toast(msg, isErr) {
    var el = $('#toast');
    el.textContent = msg;
    el.className = 'toast show' + (isErr ? ' err' : '');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { el.className = 'toast'; }, isErr ? 6000 : 2400);
  }

  function fail(err) {
    console.error(err);
    toast(err && err.message ? err.message : String(err), true);
  }

  function basename(p) { return String(p || '').split('/').filter(Boolean).pop() || p; }
  function dirname(p) { var s = String(p || '').replace(/\/+$/, '').split('/'); s.pop(); return s.join('/') || '/'; }
  function stemOf(p) { var b = basename(p); var i = b.lastIndexOf('.'); return i > 0 ? b.slice(0, i) : b; }

  /* ------------------------------------------------------------------ */
  /* 启动                                                                */
  /* ------------------------------------------------------------------ */

  function boot() {
    loadContext();
    bindTimeline();
    bindButtons();
    bindPicker();
    pollJobs();
    // 提前探测宿主桥，点击时才能同步决定用原生弹窗还是系统授权页
    loadSdk().catch(function () { /* 无宿主环境，走授权页或应用内浏览 */ });

    var params = new URLSearchParams(location.search);
    var p = params.get('path');
    if (p) {
      openFile(p).then(function () {
        if (state.cur) {
          addClip(0, state.duration, true);
        }
      }).catch(fail);
    } else {
      api('/api/context').then(function () { /* 已由 loadContext 处理 */ }).catch(function () {});
    }
  }

  function loadContext() {
    api('/api/context').then(function (ctx) {
      state.ctx = ctx;
      $('#bVersion').textContent = 'v' + (ctx.version || '—');
      $('#bFfmpeg').textContent = ctx.ffmpeg ? 'ffmpeg 可用' : 'ffmpeg 缺失';
      $('#bFfmpeg').className = 'badge ' + (ctx.ffmpeg ? 'ok' : 'err');
      $('#bUser').textContent = (ctx.user.username || '用户') + (ctx.user.isAdmin ? '（管理员）' : '');
      var oa = ctx.openApi || {};
      $('#bApi').textContent = oa.tokenPresent && oa.socketExists ? '开放 API 就绪' : '开放 API 未就绪';
      $('#bApi').className = 'badge ' + (oa.tokenPresent && oa.socketExists ? 'ok' : 'warn');
      if (!$('#outDir').value) $('#outDir').placeholder = '默认：与源文件相同目录';
    }).catch(fail);
  }

  /* ------------------------------------------------------------------ */
  /* 载入文件                                                             */
  /* ------------------------------------------------------------------ */

  function openFile(path) {
    return api('/api/file?path=' + encodeURIComponent(path)).then(function (info) {
      state.cur = info;
      state.duration = Number(info.duration) || 0;
      state.fps = fpsOf(info.video);
      state.inT = 0;
      state.outT = state.duration;
      state.keyframes = [];

      var player = $('#player');
      player.src = streamUrl(path);
      player.load();

      $('#durBadge').textContent = '时长 ' + fmtTime(state.duration, true) +
        (info.video ? ' · ' + info.video.codec + ' ' + info.video.width + 'x' + info.video.height : '');
      $('#fpsBadge').textContent = state.fps > 0 ? fmtFps(state.fps) : '帧率未知（按 25fps 步进）';
      $('#prevFrame').disabled = false;
      $('#nextFrame').disabled = false;
      syncNow(0);
      $('#kfBadge').textContent = '关键帧 读取中…';
      $('#outExt').value = info.suggestedExt === 'mkv' ? 'mkv' : 'mp4';

      if (!$('#outName').value) {
        $('#outName').value = stemOf(path) + '_cut';
      }
      syncOutDirPlaceholder(info);

      lastRulerKey = '';
      renderRuler();
      applySelection();
      renderClips();

      return api('/api/keyframes?path=' + encodeURIComponent(path)).then(function (res) {
        state.keyframes = res.keyframes || [];
        $('#kfBadge').textContent = '关键帧 ' + state.keyframes.length + ' 个';
        renderKeyframes();
      }).catch(function () {
        $('#kfBadge').textContent = '关键帧 不可用';
      });
    });
  }

  function syncOutDirPlaceholder(info) {
    var el = $('#outDir');
    el.placeholder = info && info.dir
      ? '默认：' + info.dir
      : '默认：与源文件相同目录';
  }

  /* 时间标尺（纯刻度，不使用截图） */
  var RULER_STEPS = [0.1, 0.2, 0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200];
  var lastRulerKey = '';

  function niceStep(px) {
    var d = state.duration || 0;
    if (!d || px < 40) return 0;
    var target = d * (58 / px);          // 相邻标签至少 58px，避免挤在一起
    for (var i = 0; i < RULER_STEPS.length; i++) {
      if (RULER_STEPS[i] >= target) return RULER_STEPS[i];
    }
    return RULER_STEPS[RULER_STEPS.length - 1];
  }

  function tickLabel(t, step) {
    if (step < 1) return (Math.round(t * 100) / 100) + 's';
    var h = Math.floor(t / 3600), m = Math.floor((t % 3600) / 60), s = Math.floor(t % 60);
    function p(n) { return (n < 10 ? '0' : '') + n; }
    if (h) return h + ':' + p(m) + ':' + p(s);
    if (m) return m + ':' + p(s);
    return s + 's';
  }

  function renderRuler() {
    var box = $('#ruler');
    if (!box) return;
    var d = state.duration || 0;
    var px = Math.round(box.clientWidth || 0);
    var key = px + '|' + d;
    if (key === lastRulerKey) return;
    lastRulerKey = key;
    box.innerHTML = '';
    if (!d || !px) return;

    var step = niceStep(px);
    if (!step) return;
    var minor = step / 5;
    var minorPx = px / (d / minor);
    var showMinor = minorPx >= 3;
    var n = Math.floor(d / minor + 1e-6);
    var frag = document.createDocumentFragment();

    for (var k = 0; k <= n; k++) {
      var t = k * minor;
      var left = t / d * 100;
      var isMajor = (k % 5 === 0);
      if (!isMajor && !showMinor) continue;
      var i = document.createElement('i');
      i.className = isMajor ? 'major' : 'minor';
      i.style.left = left + '%';
      if (!isMajor) { frag.appendChild(i); continue; }
      var b = document.createElement('b');
      b.textContent = tickLabel(t, step);
      b.style.left = left + '%';
      if (left <= 0.5) b.style.transform = 'none';
      else if (left >= 99.5) b.style.transform = 'translateX(-100%)';
      frag.appendChild(i);
      frag.appendChild(b);
    }
    box.appendChild(frag);
  }

  function renderKeyframes() {
    var box = $('#kfs');
    box.innerHTML = '';
    if (!state.duration || !state.keyframes.length) return;
    var max = 260;
    var kfs = state.keyframes;
    var step = Math.max(1, Math.ceil(kfs.length / max));
    for (var i = 0; i < kfs.length; i += step) {
      var el = document.createElement('i');
      el.style.left = (kfs[i] / state.duration * 100) + '%';
      box.appendChild(el);
    }
  }

  /* ------------------------------------------------------------------ */
  /* 区间选择                                                             */
  /* ------------------------------------------------------------------ */

  function clampSelection() {
    var d = state.duration || 0;
    state.inT = Math.max(0, Math.min(state.inT, d));
    state.outT = Math.max(0, Math.min(state.outT, d));
    if (state.outT < state.inT) state.outT = state.inT;
  }

  function applySelection() {
    clampSelection();
    var d = state.duration || 1;
    var a = state.inT / d * 100;
    var b = state.outT / d * 100;
    $('#sel').style.left = a + '%';
    $('#sel').style.width = Math.max(0, b - a) + '%';
    $('#hIn').style.left = a + '%';
    $('#hOut').style.left = b + '%';
    if (document.activeElement !== $('#inVal')) $('#inVal').value = state.inT.toFixed(3);
    if (document.activeElement !== $('#outVal')) $('#outVal').value = state.outT.toFixed(3);
    var span = state.outT - state.inT;
    $('#selInfo').textContent = state.duration
      ? fmtTime(state.inT, true) + ' → ' + fmtTime(state.outT, true) + '（' + fmtDur(span) + '）'
      : '未选择区间';
    var btn = $('#addClip');
    var editable = state.cur && state.active >= 0 &&
      state.clips[state.active] && state.clips[state.active].path === state.cur.path;
    btn.textContent = editable ? '更新此片段' : '剪取此区间 → 加入片段';
    btn.disabled = !state.cur || !(span > 0.001);
  }

  function bindTimeline() {
    var strip = $('#strip');

    strip.addEventListener('click', function (e) {
      if (e.target.classList.contains('handle')) return;
      var t = timeAt(e.clientX);
      var player = $('#player');
      if (player.src) player.currentTime = t;
    });

    ['hIn', 'hOut'].forEach(function (id) {
      var el = document.getElementById(id);
      el.addEventListener('pointerdown', function (e) {
        e.preventDefault();
        state.drag = id === 'hIn' ? 'in' : 'out';
        el.setPointerCapture(e.pointerId);
      });
      el.addEventListener('pointermove', function (e) {
        if (state.drag !== (id === 'hIn' ? 'in' : 'out')) return;
        var t = timeAt(e.clientX);
        if (id === 'hIn') state.inT = Math.min(t, state.outT);
        else state.outT = Math.max(t, state.inT);
        applySelection();
      });
      var end = function (e) {
        if (state.drag !== (id === 'hIn' ? 'in' : 'out')) return;
        state.drag = null;
        try { el.releasePointerCapture(e.pointerId); } catch (err) {}
        if (id === 'hIn' && state.snap) snapIn();
      };
      el.addEventListener('pointerup', end);
      el.addEventListener('pointercancel', end);
    });

    var player = $('#player');
    player.addEventListener('timeupdate', function () {
      syncNow(player.currentTime);
    });
    player.addEventListener('loadedmetadata', function () {
      if (!state.duration && player.duration && isFinite(player.duration)) {
        state.duration = player.duration;
        state.outT = state.duration;
        lastRulerKey = '';
        renderRuler();
        applySelection();
      }
    });
    player.addEventListener('error', function () {
      toast('无法播放该文件（可仍然进行无损导出）', true);
    });

    $('#inVal').addEventListener('change', function () {
      state.inT = parseFloat(this.value) || 0;
      if (state.snap) snapIn();
      applySelection();
    });
    $('#outVal').addEventListener('change', function () {
      state.outT = parseFloat(this.value) || 0;
      applySelection();
    });
    $('#setIn').addEventListener('click', function () {
      state.inT = $('#player').currentTime || 0;
      if (state.snap) snapIn();
      applySelection();
    });
    $('#setOut').addEventListener('click', function () {
      state.outT = $('#player').currentTime || 0;
      applySelection();
    });
    $('#snap').addEventListener('change', function () {
      state.snap = this.checked;
    });
    $('#addClip').addEventListener('click', function () {
      addClip(state.inT, state.outT, false);
    });

    // 逐帧定位
    $('#prevFrame').addEventListener('click', function () { stepFrame(-1); });
    $('#nextFrame').addEventListener('click', function () { stepFrame(1); });
    document.addEventListener('keydown', function (e) {
      if (e.ctrlKey || e.metaKey || e.altKey) return;
      if (e.key !== 'ArrowLeft' && e.key !== 'ArrowRight') return;
      var el = document.activeElement;
      var tag = el && el.tagName ? el.tagName.toLowerCase() : '';
      if (tag === 'input' || tag === 'textarea' || tag === 'select') return;
      if ($('#picker') && $('#picker').open) return;
      if (!state.cur) return;
      e.preventDefault();
      // Shift + 方向键 = 1 秒；单独方向键 = 1 帧
      stepFrame(e.key === 'ArrowLeft' ? -1 : 1, e.shiftKey ? 1 : 0);
    });
    $('#btnRefresh').addEventListener('click', function () {
      if (state.cur) openFile(state.cur.path).catch(fail);
    });

    // 宽度变化时重排刻度（窗口拉动、最大化、进入/退出全屏）
    if (window.ResizeObserver) {
      try { new ResizeObserver(function () { renderRuler(); }).observe(strip); } catch (e) {}
    } else {
      window.addEventListener('resize', function () { renderRuler(); });
    }
  }

  function timeAt(clientX) {
    var rect = $('#strip').getBoundingClientRect();
    var ratio = (clientX - rect.left) / (rect.width || 1);
    ratio = Math.max(0, Math.min(1, ratio));
    return ratio * (state.duration || 0);
  }

  function snapIn() {
    if (!state.keyframes.length) return;
    var best = 0;
    for (var i = 0; i < state.keyframes.length; i++) {
      if (state.keyframes[i] <= state.inT + 1e-6) best = state.keyframes[i];
      else break;
    }
    state.inT = best;
  }

  /* ------------------------------------------------------------------ */
  /* 片段列表                                                             */
  /* ------------------------------------------------------------------ */

  function addClip(start, end, silent) {
    if (!state.cur) return;
    var clip = {
      path: state.cur.path,
      name: state.cur.name,
      duration: state.duration,
      start: Math.max(0, Number(start) || 0),
      end: Math.max(0, Number(end) || state.duration)
    };
    if (state.active >= 0 && state.clips[state.active] && state.clips[state.active].path === clip.path) {
      state.clips[state.active] = clip;
      if (!silent) toast('已更新该片段区间');
    } else {
      state.clips.push(clip);
      state.active = state.clips.length - 1;
      if (!silent) toast('已加入片段列表');
    }
    renderClips();
    schedulePreflight();
  }

  function renderClips() {
    var box = $('#clipList');
    box.innerHTML = '';
    $('#clipCount').textContent = state.clips.length;
    $('#clipEmpty').style.display = state.clips.length ? 'none' : 'block';
    $('#btnExport').disabled = !state.clips.length;

    state.clips.forEach(function (c, i) {
      var span = Math.max(0, c.end - c.start);
      var el = document.createElement('div');
      el.className = 'clip' + (i === state.active ? ' active' : '');
      el.innerHTML =
        '<img class="thumb" src="' + esc(thumbUrl(c.path, c.start + Math.min(1, span / 2), 128)) + '" alt="">' +
        '<div style="min-width:0">' +
          '<div class="nm" title="' + esc(c.path) + '">' + esc(c.name) + '</div>' +
          '<div class="meta">' + fmtTime(c.start, true) + ' → ' + fmtTime(c.end, true) +
            '（' + fmtDur(span) + '）</div>' +
        '</div>' +
        '<div class="ops">' +
          '<button class="ghost" data-act="up" title="上移">↑</button>' +
          '<button class="ghost" data-act="down" title="下移">↓</button>' +
          '<button class="ghost" data-act="edit" title="载入时间轴">✎</button>' +
          '<button class="ghost danger" data-act="del" title="移除">✕</button>' +
        '</div>';
      el.addEventListener('click', function (e) {
        var act = e.target.getAttribute && e.target.getAttribute('data-act');
        if (!act) {
          state.active = i;
          loadClipToTimeline(i);
          return;
        }
        e.stopPropagation();
        if (act === 'up' && i > 0) move(i, i - 1);
        else if (act === 'down' && i < state.clips.length - 1) move(i, i + 1);
        else if (act === 'edit') loadClipToTimeline(i);
        else if (act === 'del') {
          state.clips.splice(i, 1);
          if (state.active === i) state.active = -1;
          else if (state.active > i) state.active -= 1;
          renderClips();
          schedulePreflight();
        }
      });
      box.appendChild(el);
    });
  }

  function move(from, to) {
    var c = state.clips.splice(from, 1)[0];
    state.clips.splice(to, 0, c);
    if (state.active === from) state.active = to;
    renderClips();
    schedulePreflight();
  }

  function loadClipToTimeline(i) {
    var c = state.clips[i];
    if (!c) return;
    state.active = i;
    if (state.cur && state.cur.path === c.path) {
      state.inT = c.start;
      state.outT = c.end;
      applySelection();
      renderClips();
      return;
    }
    openFile(c.path).then(function () {
      state.inT = c.start;
      state.outT = c.end;
      applySelection();
      renderClips();
    }).catch(fail);
  }

  /* ------------------------------------------------------------------ */
  /* 导出前检查                                                           */
  /* ------------------------------------------------------------------ */

  function schedulePreflight() {
    clearTimeout(state.pfTimer);
    state.pfTimer = setTimeout(runPreflight, 400);
  }

  function runPreflight() {
    var box = $('#preflight');
    if (!state.clips.length) { box.innerHTML = ''; return; }
    var payload = {
      clips: state.clips.map(function (c) { return { path: c.path, start: c.start, end: c.end }; }),
      snapKeyframes: state.snap
    };
    api('/api/preflight', { method: 'POST', body: payload }).then(function (pf) {
      var html = '';
      if (pf.warnings && pf.warnings.length) {
        html += '<div class="warnbox">' + pf.warnings.map(esc).join('<br>') + '</div>';
      }
      html += '<div class="row tight" style="margin-bottom:6px">' +
        '<span class="badge">合计 ' + fmtDur(pf.totalDuration) + '</span>' +
        '<span class="badge">' + pf.plans.length + ' 段</span>' +
        '<span class="badge ' + (pf.compatible ? 'ok' : 'warn') + '">' +
        (pf.compatible ? '参数一致，可无损合并' : '参数不一致') + '</span></div>';
      box.innerHTML = html;
      if (pf.suggestedExt && !$('#outExt').value) $('#outExt').value = pf.suggestedExt;
      state.preflight = pf;
    }).catch(function (e) {
      box.innerHTML = '<div class="warnbox">' + esc(e.message) + '</div>';
    });
  }

  /* ------------------------------------------------------------------ */
  /* 导出与任务                                                           */
  /* ------------------------------------------------------------------ */

  function doExport() {
    if (!state.clips.length) return;
    $('#btnExport').disabled = true;
    var ext = $('#outExt').value || 'mkv';
    var name = ($('#outName').value || '').trim();
    var options = {
      outDir: ($('#outDir').value || '').trim(),
      filename: name || '',
      ext: ext,
      snapKeyframes: state.snap,
      keepAllStreams: $('#optKeepAll').checked,
      force: $('#optForce').checked
    };
    var payload = {
      clips: state.clips.map(function (c) { return { path: c.path, start: c.start, end: c.end }; }),
      options: options
    };
    api('/api/jobs', { method: 'POST', body: payload }).then(function (job) {
      toast('已开始导出：' + job.output.name);
      state.jobs.unshift(job);
      renderJobs();
      pollJobs(true);
    }).catch(fail).then(function () {
      $('#btnExport').disabled = false;
    });
  }

  function pollJobs(immediate) {
    if (state.timer) clearTimeout(state.timer);
    var refresh = function () {
      api('/api/jobs').then(function (res) {
        state.jobs = res.jobs || [];
        renderJobs();
        var running = state.jobs.some(function (j) {
          return j.state === 'running' || j.state === 'pending';
        });
        state.timer = setTimeout(refresh, running ? 1000 : 5000);
      }).catch(function () {
        state.timer = setTimeout(refresh, 8000);
      });
    };
    if (immediate) refresh();
    else state.timer = setTimeout(refresh, 300);
  }

  var STATE_TEXT = {
    pending: '排队中', running: '进行中', done: '已完成',
    failed: '失败', canceled: '已取消', interrupted: '已中断'
  };
  var STATE_CLASS = {
    pending: '', running: '', done: 'ok', failed: 'err', canceled: 'warn', interrupted: 'warn'
  };

  function renderJobs() {
    var box = $('#jobs');
    box.innerHTML = '';
    $('#jobCount').textContent = state.jobs.length;
    if (!state.jobs.length) {
      box.innerHTML = '<div class="empty">暂无任务</div>';
      return;
    }
    state.jobs.forEach(function (j) {
      var el = document.createElement('div');
      el.className = 'job';
      var title = (j.clips && j.clips.length > 1 ? '合并 ' + j.clips.length + ' 段 · ' : '剪切 · ') +
        esc(j.output ? j.output.name : '');
      var html = '<div class="t">' +
        '<span class="badge ' + (STATE_CLASS[j.state] || '') + '">' + (STATE_TEXT[j.state] || j.state) + '</span>' +
        '<span class="mono">' + title + '</span>' +
        '<div class="spacer"></div>' +
        '<span class="hint">' + new Date(j.createdAt * 1000).toLocaleString('zh-CN') + '</span>' +
        '</div>';
      if (j.state === 'running' || j.state === 'pending') {
        html += '<div class="bar" style="margin-top:6px"><i style="width:' + (j.progress || 0) + '%"></i></div>' +
          '<div class="hint">' + esc(j.message || '') + ' ' + (j.progress || 0).toFixed(1) + '%</div>';
      } else if (j.state === 'done') {
        html += '<div class="p">' + esc(j.output ? j.output.path : '') +
          (j.output && j.output.size ? '　' + fmtSize(j.output.size) : '') + '</div>';
      } else if (j.error) {
        html += '<div class="err">' + esc(j.error) + '</div>';
      } else {
        html += '<div class="p">' + esc(j.message || '') + '</div>';
      }
      html += '<div class="row tight" style="margin-top:6px">';
      if (j.state === 'done') {
        html += '<a href="' + esc(downloadUrl(j.output.path)) + '" download><button class="ghost">下载</button></a>' +
          '<button class="ghost" data-act="copy">复制路径</button>';
      }
      if (j.state === 'running' || j.state === 'pending') {
        html += '<button class="ghost danger" data-act="cancel">取消</button>';
      } else {
        html += '<button class="ghost" data-act="remove">移除记录</button>';
      }
      html += '</div>';
      el.innerHTML = html;
      el.addEventListener('click', function (e) {
        var act = e.target.getAttribute && e.target.getAttribute('data-act');
        if (!act) return;
        e.stopPropagation();
        if (act === 'cancel') {
          api('/api/jobs/' + j.id + '/cancel', { method: 'POST' }).then(function () { toast('已请求取消'); }).catch(fail);
        } else if (act === 'remove') {
          api('/api/jobs/' + j.id, { method: 'DELETE' }).then(function () { pollJobs(true); }).catch(fail);
        } else if (act === 'copy') {
          var p = j.output && j.output.path;
          if (p && navigator.clipboard) {
            navigator.clipboard.writeText(p).then(function () { toast('已复制路径'); }, function () { toast(p, false); });
          } else {
            toast(p || '', false);
          }
        }
      });
      box.appendChild(el);
    });
  }

  /* ------------------------------------------------------------------ */
  /* ------------------------------------------------------------------ */
  /* 系统原生选择器（fnOS 宿主 JS SDK）                                    */
  /* ------------------------------------------------------------------ */

  // 与后端 media.VIDEO_EXTS 保持一致（不带点的扩展名）
  var VIDEO_EXTS = ['mp4', 'mkv', 'mov', 'avi', 'flv', 'ts', 'm2ts', 'mts', 'webm', 'wmv',
    'mpg', 'mpeg', 'm4v', '3gp', 'vob', 'rmvb', 'rm', 'asf', 'ogv', 'divx', 'f4v', 'm2v',
    'mxf', 'dv', 'trp', 'tp', 'm3u8', 'mp2', 'mpe', 'mpv', 'dat'];

  // 系统文件选择器的 accept 过滤规则（/usr/trim/www/assets/index-B0cXg_s1.js 中的 $6）：
  //   wo(path) 取小写扩展名（不带点），再判断 accept.includes('.' + ext)
  // 因此 accept 里的每一项都必须带前导点；官方文档写的“不带点”与实现不符，
  // 传不带点的列表会导致所有文件在选择器里变灰、无法选中。
  var VIDEO_ACCEPT = VIDEO_EXTS.map(function (e) { return '.' + e; });

  var APPNAME = 'videocut';

  var sdkPromise = null;
  var sdkState = { ready: false, usable: false };
  var sdkLogs = [];
  var pendingAuth = null;
  var authTimer = null;

  /* ---- 诊断：前端把关键节点写进服务端日志，便于在真机上定位问题 ---- */

  function describe(v) {
    try {
      if (v === null) return 'null';
      if (v === undefined) return 'undefined';
      if (Array.isArray(v)) return 'array(' + v.length + ')';
      if (typeof v === 'object') return 'object{' + Object.keys(v).slice(0, 12).join(',') + '}';
      return typeof v + ':' + String(v).slice(0, 80);
    } catch (e) { return 'unreadable'; }
  }

  function pushLog(s) {
    try {
      sdkLogs.push(String(s).slice(0, 400));
      if (sdkLogs.length > 120) sdkLogs.shift();
    } catch (e) { /* ignore */ }
  }

  function clientlog(kind, data) {
    var payload = data;
    try {
      payload = {};
      for (var k in data) if (Object.prototype.hasOwnProperty.call(data, k)) payload[k] = data[k];
      payload.logs = sdkLogs.slice(-60);
    } catch (e) { /* ignore */ }
    try {
      api('/api/clientlog', { method: 'POST', body: { kind: kind, data: payload } })
        .catch(function () { /* 诊断失败不影响主流程 */ });
    } catch (e) { /* ignore */ }
  }

  // 把 SDK 自己的 console 输出也收进诊断缓冲
  ['log', 'debug', 'info', 'warn', 'error'].forEach(function (level) {
    var orig = console[level];
    if (typeof orig !== 'function') return;
    console[level] = function () {
      try {
        pushLog(level + ': ' + Array.prototype.map.call(arguments, function (a) {
          return typeof a === 'string' ? a : describe(a);
        }).join(' '));
      } catch (e) { /* ignore */ }
      return orig.apply(console, arguments);
    };
  });

  window.addEventListener('error', function (ev) {
    clientlog('window-error', {
      message: String(ev.message || ''),
      at: String(ev.filename || '') + ':' + (ev.lineno || 0)
    });
  });
  window.addEventListener('unhandledrejection', function (ev) {
    var r = ev.reason;
    clientlog('unhandled-rejection', { message: String((r && r.message) || r) });
  });

  function loadSdk() {
    if (!sdkPromise) {
      sdkPromise = import(PREFIX + '/vendor/trim-app.js').then(function (mod) {
        var sdk = new mod.TrimApp({ debug: true });
        return Promise.resolve(sdk.initPromise).then(function () {
          sdkState = {
            ready: true,
            usable: !!(sdk.isWeb && !sdk.isStandaloneWeb && typeof sdk.pickUserFile === 'function'),
            isWeb: !!sdk.isWeb,
            isStandaloneWeb: !!sdk.isStandaloneWeb,
            openAppAuth: typeof sdk.openAppAuth
          };
          clientlog('sdk-ready', sdkState);
          return sdk;
        });
      }).catch(function (e) {
        sdkState = { ready: true, usable: false, error: String((e && e.message) || e) };
        clientlog('sdk-load-failed', sdkState);
        sdkPromise = null;
        throw e;
      });
    }
    return sdkPromise;
  }

  // 用户在系统选择器里点「取消」时，宿主桥不抛错，而是返回
  // {code:<非 0>, msg:'Cancelled', data:[]}（系统 bundle 里的 K7）。
  // 这属于正常操作，必须静默处理，绝不能报成「选择器不可用」。
  function looksCancelled(v) {
    return /cancel|取消/i.test(String((v && v.msg) || (v && v.message) || v || ''));
  }

  function cancelError() {
    var e = new Error('用户取消');
    e.cancelled = true;
    return e;
  }

  function isCancelled(v) {
    return !!(v && v.cancelled) || looksCancelled(v);
  }

  // 从宿主可能返回的几种形状里取出路径数组；null = 当前环境没有宿主编桥
  function extractPaths(res) {
    if (res === null || res === undefined) return null;
    if (Array.isArray(res)) return res.slice();
    if (res.code && res.code !== 0) {
      // 宿主桥把「用户取消」和「选完但授权失败」压成同一个形状（系统 bundle 的 K7：
      // {code:1, msg:'Operation failed', data:[]}，真机上取消时 msg 并不会是 'Cancelled'），
      // 两者无法区分。所以按「这次没有选到视频」处理：默认静默，
      // 只有宿主给出真实原因文案时才提示。真正的故障会以 reject 抛出，不走这里。
      var m = String(res.msg || '');
      clientlog('pick-nonzero', { code: res.code, msg: m, cancelled: looksCancelled(res) });
      if (m && !/^(operation failed|cancell?ed)$/i.test(m)) {
        var e = new Error(m);
        e.noSelection = true;
        throw e;
      }
      return [];
    }
    if (Array.isArray(res.data)) return res.data.slice();
    if (res.data && Array.isArray(res.data.paths)) return res.data.paths.slice();
    if (Array.isArray(res.paths)) return res.paths.slice();
    return [];
  }

  // 调试钩子：window.__VC_FAKE_PICK__ = ["/path/a.mp4"] 模拟宿主选择器的「原始」返回值
  // （即宿主桥的 {code,msg,data} 形状，因此会走和真实返回值完全一样的解析与取消判定）
  function nativePick(params) {
    if (window.__VC_FAKE_PICK__ !== undefined) {
      var fake = window.__VC_FAKE_PICK__;
      return Promise.resolve(fake === true ? null : fake).then(function (v) {
        clientlog('pick-fake', { shape: describe(v) });
        return extractPaths(v);
      });
    }
    return loadSdk().then(function (sdk) {
      sdkState.usable = !!(sdk.isWeb && !sdk.isStandaloneWeb && typeof sdk.pickUserFile === 'function');
      if (!sdkState.usable) return null;
      return Promise.resolve(sdk.pickUserFile(params)).then(function (res) {
        clientlog('pick-raw', {
          shape: describe(res),
          code: (res && res.code) || 0,
          msg: (res && res.msg) || ''
        });
        return extractPaths(res);
      }, function (err) {
        clientlog('pick-rejected', {
          error: String((err && err.message) || err),
          cancelled: isCancelled(err)
        });
        throw isCancelled(err) ? cancelError() : err;
      });
    });
  }

  function authorize(paths) {
    return api('/api/authorize', { method: 'POST', body: { paths: paths } });
  }

  function addWholeFiles(paths) {
    return Promise.all(paths.map(function (p) {
      return api('/api/file?path=' + encodeURIComponent(p))
        .then(function (info) { return { path: p, info: info }; })
        .catch(function () { return null; });
    })).then(function (rows) {
      var added = 0;
      rows.forEach(function (row) {
        if (!row || !row.info) return;
        var d = Number(row.info.duration) || 0;
        state.clips.push({
          path: row.path,
          name: row.info.name || basename(row.path),
          duration: d,
          start: 0,
          end: d
        });
        added++;
      });
      if (added) {
        renderClips();
        schedulePreflight();
      }
      return added;
    });
  }

  function browseFallback() {
    openPicker('file', (state.cur && state.cur.dir) || (state.ctx && state.ctx.shareDir) || '',
      function (p) { openFile(p).catch(fail); });
  }

  function onAuthorized(res) {
    var accepted = res.accepted || [];
    if (res.rejected && res.rejected.length) {
      toast(accepted.length + ' 项已接受，' + res.rejected.length + ' 项被拒：' +
        (res.rejected[0].reason || ''), true);
    }
    var dirs = accepted.filter(function (a) { return a.type === 'dir'; });
    var files = accepted.filter(function (a) { return a.type === 'file'; });
    if (dirs.length) loadContext();
    if (!files.length) return null;
    var paths = files.map(function (f) { return f.path; });
    if (paths.length === 1 && !state.clips.length) {
      return openFile(paths[0]).catch(fail);
    }
    return addWholeFiles(paths).then(function (n) {
      if (n) toast('已加入 ' + n + ' 个片段（整段）');
    });
  }

  /* ---- 系统授权页：不依赖宿主编桥的备用通道 ---- */

  function randState() {
    return Math.random().toString(36).slice(2) + Date.now().toString(36);
  }

  function callbackUrl() {
    return location.origin + PREFIX + '/callback.html';
  }

  // 直接打开飞牛系统授权页 /app-auth/pick-user-file，选完由 callback.html 回传结果。
  // 必须在用户点击的同步调用栈里执行，否则会被浏览器的弹窗拦截判定为非法。
  function authPagePick(mode) {
    var st = randState();
    var url = location.origin + '/app-auth/pick-user-file' +
      '?appName=' + encodeURIComponent(APPNAME) +
      '&redirect_uri=' + encodeURIComponent(callbackUrl()) +
      '&state=' + encodeURIComponent(st) +
      '&directory=' + (mode === 'dir' ? 'true' : 'false');
    if (mode !== 'dir') url += '&accept=' + encodeURIComponent(VIDEO_ACCEPT.join(','));
    var win = null;
    try { win = window.open(url, 'vc_pick_' + st, 'width=1024,height=720'); } catch (e) { win = null; }
    clientlog('authpage-open', { mode: mode, opened: !!win, url: url });
    if (!win) {
      toast('浏览器拦截了新窗口，请允许本站弹出窗口后重试', true);
      return false;
    }
    pendingAuth = { state: st, mode: mode, at: Date.now() };
    startAuthWatch();
    return true;
  }

  function stopAuthWatch() {
    if (authTimer) { clearInterval(authTimer); authTimer = null; }
    window.removeEventListener('message', onAuthMessage);
  }

  function startAuthWatch() {
    stopAuthWatch();
    window.addEventListener('message', onAuthMessage);
    authTimer = setInterval(function () {
      if (!pendingAuth) { stopAuthWatch(); return; }
      var raw = null;
      try { raw = localStorage.getItem('vc.pick.result'); } catch (e) { raw = null; }
      if (raw) {
        try { localStorage.removeItem('vc.pick.result'); } catch (e) { /* ignore */ }
        try { handleAuthResult(JSON.parse(raw)); } catch (e) {
          clientlog('authpage-parse-failed', { error: String(e) });
        }
      }
      if (pendingAuth && Date.now() - pendingAuth.at > 10 * 60 * 1000) {
        pendingAuth = null;
        stopAuthWatch();
      }
    }, 600);
  }

  function onAuthMessage(ev) {
    if (ev.origin !== location.origin) return;
    var d = ev.data;
    if (!d || d.type !== 'vc-pick') return;
    clientlog('authpage-message', { status: d.status, count: (d.paths || []).length });
    handleAuthResult(d);
  }

  function handleAuthResult(payload) {
    var pend = pendingAuth;
    if (!pend) return;
    if (payload && payload.state && pend.state && payload.state !== pend.state) {
      clientlog('authpage-state-mismatch', { got: payload.state });
      return;
    }
    pendingAuth = null;
    stopAuthWatch();
    var paths = ((payload && payload.paths) || []).slice();
    clientlog('authpage-result', {
      mode: pend.mode, status: payload && payload.status, count: paths.length
    });
    var st = (payload && payload.status) || '';
    if (st === 'cancel' || st === 'cancelled' || (payload && payload.error === 'access_denied')) {
      clientlog('authpage-cancelled', { status: st });   // 用户主动取消：静默
      return;
    }
    if (st !== 'success' || !paths.length) {
      toast('没有选择任何项目');
      return;
    }
    return applyPicked(pend.mode, paths);
  }

  // 选择结果统一从这里落地：先向后端登记，再加入片段 / 设为输出目录
  function applyPicked(mode, paths) {
    return authorize(paths).then(function (res) {
      if (mode === 'dir') {
        var d = (res.accepted || [])[0];
        if (d) { $('#outDir').value = d.path; toast('输出目录已设为 ' + d.path); }
        return loadContext();
      }
      return onAuthorized(res);
    }).catch(fail);
  }

  /* ---- 选择入口 ---- */

  function pickVideos() {
    if (!sdkState.usable && window.__VC_FAKE_PICK__ === undefined) {
      if (authPagePick('file')) return;
      browseFallback();
      return;
    }
    return nativePick({
      directory: false,
      multiple: true,
      accept: VIDEO_ACCEPT,
      title: '选择视频',
      okText: '添加'
    }).then(function (paths) {
      if (paths === null) {
        if (authPagePick('file')) return;
        browseFallback();
        return;
      }
      if (!paths.length) { toast('没有选择任何视频'); return; }
      return applyPicked('file', paths);
    }).catch(function (e) {
      if (isCancelled(e)) { clientlog('pick-cancelled', {}); return; }
      if (e && e.noSelection) {
        clientlog('pick-none', { detail: String((e && e.message) || '') });
        toast('没有选择视频：' + ((e && e.message) || ''));
        return;
      }
      clientlog('pick-failed', { error: String((e && e.message) || e) });
      toast('选择视频失败：' + ((e && e.message) || e) + '。可以再点一次「选择视频…」，或改用「授权目录…」', true);
    });
  }

  function pickOutDir() {
    var cur = ($('#outDir').value || '').trim() ||
      (state.cur && state.cur.dir) || (state.ctx && state.ctx.shareDir) || '';
    if (!sdkState.usable && window.__VC_FAKE_PICK__ === undefined) {
      if (authPagePick('dir')) return;
      openPicker('dir', cur, function (p) { $('#outDir').value = p; });
      return;
    }
    return nativePick({
      directory: true,
      title: '选择输出目录',
      okText: '使用此目录',
      sidebarGroup: ['myFiles', 'otherShare', 'favorites']
    }).then(function (paths) {
      if (paths === null) {
        if (authPagePick('dir')) return;
        openPicker('dir', cur, function (p) { $('#outDir').value = p; });
        return;
      }
      if (!paths.length) return;
      return applyPicked('dir', paths);
    }).catch(function (e) {
      if (isCancelled(e)) { clientlog('pick-dir-cancelled', {}); return; }
      if (e && e.noSelection) { clientlog('pick-dir-none', { detail: String((e && e.message) || '') }); return; }
      clientlog('pick-dir-failed', { error: String((e && e.message) || e) });
      openPicker('dir', cur, function (p) { $('#outDir').value = p; });
    });
  }

  /* ------------------------------------------------------------------ */
  /* 文件选择器                                                           */
  /* ------------------------------------------------------------------ */

  function bindPicker() {
    var dlg = $('#picker');
    $('#pickCancel').addEventListener('click', function () { dlg.close(); });
    $('#pickUp').addEventListener('click', function () {
      if (state.picker.parent) loadPicker(state.picker.parent);
    });
    $('#pickOk').addEventListener('click', function () {
      if (state.picker.onlyDirs) {
        var cb = state.picker.onPick;
        dlg.close();
        if (cb) cb(state.picker.path);
      } else {
        dlg.close();
      }
    });
  }

  function openPicker(mode, start, onPick) {
    state.picker.mode = mode;
    state.picker.onPick = onPick;
    state.picker.onlyDirs = mode === 'dir';
    $('#pickTitle').textContent = mode === 'dir' ? '选择输出目录' : '选择视频文件';
    $('#pickOk').style.display = mode === 'dir' ? '' : 'none';
    var roots = (state.ctx && state.ctx.roots) || [];
    var fallback = roots.length ? roots[0].path : '/';
    loadPicker(start || fallback);
    $('#picker').showModal();
  }

  function loadPicker(path) {
    var onlyDirs = state.picker.onlyDirs ? '1' : '0';
    api('/api/browse?dirs=' + onlyDirs + '&path=' + encodeURIComponent(path)).then(function (res) {
      state.picker.path = res.path;
      state.picker.parent = res.parent || '';
      $('#pickPath').textContent = res.path;
      var box = $('#pickList');
      box.innerHTML = '';
      if (!res.dirs.length && !res.files.length) {
        box.innerHTML = '<div class="empty">该目录下没有可选内容</div>';
      }
      res.dirs.forEach(function (d) {
        var el = document.createElement('div');
        el.className = 'entry';
        el.innerHTML = '<span class="ic">📁</span><span>' + esc(d.name) + '</span>';
        el.addEventListener('click', function () { loadPicker(d.path); });
        box.appendChild(el);
      });
      res.files.forEach(function (f) {
        var el = document.createElement('div');
        el.className = 'entry' + (f.readable ? '' : ' disabled');
        el.innerHTML = '<span class="ic">🎬</span><span>' + esc(f.name) + '</span>' +
          '<span class="sz">' + fmtSize(f.size) + '</span>';
        el.addEventListener('click', function () {
          if (!f.readable) { toast('应用无权读取该文件，请先在文件管理器里授权', true); return; }
          $('#picker').close();
          var cb = state.picker.onPick;
          if (cb) cb(f.path);
        });
        box.appendChild(el);
      });
      // 根目录快捷入口
      var roots = (state.ctx && state.ctx.roots) || [];
      if (roots.length) {
        var bar = document.createElement('div');
        bar.className = 'row tight';
        bar.style.marginBottom = '8px';
        roots.forEach(function (r) {
          var b = document.createElement('button');
          b.className = 'ghost';
          b.textContent = r.label || r.path;
          b.addEventListener('click', function () { loadPicker(r.path); });
          bar.appendChild(b);
        });
        box.insertBefore(bar, box.firstChild);
      }
    }).catch(function (e) {
      $('#pickList').innerHTML = '<div class="empty">' + esc(e.message) + '</div>';
    });
  }

  /* ------------------------------------------------------------------ */
  /* 按钮绑定                                                             */
  /* ------------------------------------------------------------------ */

  function bindButtons() {
    $('#btnPick').addEventListener('click', pickVideos);
    $('#btnBrowse').addEventListener('click', browseFallback);
    $('#btnOutDir').addEventListener('click', pickOutDir);
    $('#clearClips').addEventListener('click', function () {
      state.clips = [];
      state.active = -1;
      renderClips();
      schedulePreflight();
    });
    $('#btnExport').addEventListener('click', doExport);
    window.addEventListener('beforeunload', function () {
      if (state.jobs.some(function (j) { return j.state === 'running'; })) {
        return '仍有导出任务在进行中，关闭页面不会中断任务，但会看不到进度。';
      }
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();
