(function () {
  'use strict';

  var $ = function (id) { return document.getElementById(id); };
  var pullTimer = null;
  var bridgeTimer = null;
  var publishTimer = null;
  var SIDEBAR_KEY = 'dzmm-console-sidebar-collapsed';
  var AGENT_KEY = 'dzmm-console-agent-collapsed';
  var CONSOLE_MODE_KEY = 'dzmm-console-mode';
  var CONSOLE_CARD_KEY = 'dzmm-console-card-id';
  var consoleMode = 'game'; // game | card
  var lastGamePanel = 'login';
  var FAB_POS_KEY = 'dzmm-console-fab-pos';
  var FAB_CIRC = 2 * Math.PI * 20; // r=20
  var currentCardId = '';
  var currentCard = null;
  var currentCardMtime = 0;
  var currentCardSig = '';
  var currentCardFolder = '';
  var cardPollTimer = null;
  var cardPollBusy = false;
  var lastDiskFieldValues = Object.create(null);
  var CARD_POLL_MS = 200;
  var cardListPollTimer = null;
  var cardListPollBusy = false;
  var cardListPollTick = 0;
  var lastLocalListSig = '';
  var lastCloudListSig = '';
  var cardApplyingRemote = false;
  var voiceCatalog = { public: [], mine: [] };
  var voiceAudio = null;
  var cloudCardItems = [];
  var cloudCardFilter = 'all'; // all | draft | pub
  var cloudCardSearch = '';
  var cloudVisibleCount = 20;
  var cardPlayMode = false;
  var playState = {
    chatId: '',
    cardId: 0,
    messages: [],
    sending: false,
    modelsLoaded: false,
    presetsLoaded: false,
    defaultModel: '',
    modelGroups: [],
    selectedGroupKey: '',
    selectedModel: '',
    charName: '',
    userName: '',
    playerInfo: '',
    presetCatalog: [],
    selectedPresetIds: [],
    // 会话设置（chat.getSettings / updateSettings）
    title: '会话',
    style: 'standard',
    maxTokens: 2500,
    imageGenerationModel: 'anime',
    enableHighlight: false,
    classicUi: false,
  };
  var CLOUD_PAGE_SIZE = 20;
  var cloudSearchTimer = null;
  var previewSource = 'local';
  var cloudPreviewTimer = null;
  var cloudPreviewRevision = 0;

  function previewEmbedUrl(url) {
    if (!url) return '';
    try {
      var u = new URL(url, window.location.href);
      u.searchParams.set('embed', '1');
      return u.toString();
    } catch (_) {
      return url + (url.indexOf('?') >= 0 ? '&' : '?') + 'embed=1';
    }
  }

  function setPreviewSourceUi(source) {
    previewSource = source === 'cloud' ? 'cloud' : 'local';
    if ($('previewSrcLocal')) $('previewSrcLocal').classList.toggle('active', previewSource === 'local');
    if ($('previewSrcCloud')) $('previewSrcCloud').classList.toggle('active', previewSource === 'cloud');
    if ($('previewTitle')) $('previewTitle').textContent = previewSource === 'cloud' ? '云端预览' : '本地预览';
  }

  function stopCloudPreviewPoll() {
    if (cloudPreviewTimer) {
      clearInterval(cloudPreviewTimer);
      cloudPreviewTimer = null;
    }
  }

  var cloudPreviewLastReloadAt = 0;
  var cloudPreviewReloadPending = false;

  function previewBaseKey(url) {
    if (!url) return '';
    try {
      var u = new URL(url, window.location.href);
      u.searchParams.delete('_ts');
      u.searchParams.delete('embed');
      return u.origin + u.pathname;
    } catch (_) {
      return String(url).replace(/[?&](_ts|embed)=[^&]*/g, '').replace(/[?&]$/, '');
    }
  }

  function reloadPreviewFrame(force) {
    var frame = $('previewFrame');
    if (!frame) return;
    var base = frame.dataset.url || '';
    if (!base || base === 'about:blank') return;
    var now = Date.now();
    // 避免云端轮询把整页刷爆（会打穿 static 代理限流 → 429）
    if (!force && now - cloudPreviewLastReloadAt < 8000) return;
    cloudPreviewLastReloadAt = now;
    frame.src = base + (base.indexOf('?') >= 0 ? '&' : '?') + '_ts=' + now;
  }

  function startCloudPreviewPoll() {
    stopCloudPreviewPoll();
    if (previewSource !== 'cloud') return;
    cloudPreviewTimer = setInterval(async function () {
      if (previewSource !== 'cloud') return;
      try {
        var data = await api('/api/preview/source');
        var cloud = data.cloud || (data.preview && data.preview.cloud) || {};
        var rev = Number(cloud.revision || 0);
        var meta = $('previewMeta');
        var tip = cloud.message || '云端镜像中';
        if (cloud.running) tip = '正在拉取云端…';
        if (cloud.error) tip = cloud.error;
        if (data.pending) tip = tip || '首次拉取中…';
        if (meta) {
          meta.textContent = '云端预览 · rev ' + rev + ' · ' + tip;
        }
        if (data.source) setPreviewSourceUi(data.source);
        var frame = $('previewFrame');
        var curKey = previewBaseKey(frame && frame.dataset.url);
        var nextUrl = (data.preview && data.preview.url) || '';
        var nextKey = previewBaseKey(nextUrl);
        var actual = (data.preview && data.preview.source) || '';
        // 仅在「尚未挂上云端预览」时启动一次，勿每轮 render（会带 _ts 整页重载）
        if (data.preview && data.preview.running && actual === 'cloud' && nextKey && curKey !== nextKey) {
          renderPreview(data.preview, { keepSourceUi: true });
        } else if (data.preview && !data.preview.running && data.pending) {
          renderPreview(data.preview, { keepSourceUi: true });
        }
        // 镜像有变更：拉取中只记标记，结束后再刷一次，避免整页连刷打穿限流
        if (rev > cloudPreviewRevision) {
          cloudPreviewRevision = rev;
          if (cloud.running || data.pending) cloudPreviewReloadPending = true;
          else if (curKey) {
            cloudPreviewReloadPending = false;
            reloadPreviewFrame(false);
          }
        } else if (cloudPreviewReloadPending && !cloud.running && !data.pending && curKey) {
          cloudPreviewReloadPending = false;
          reloadPreviewFrame(false);
        }
      } catch (_) {}
    }, 4000);
  }

  async function switchPreviewSource(source) {
    source = source === 'cloud' ? 'cloud' : 'local';
    setBusy(true);
    showMsg(source === 'cloud' ? '正在切换到云端预览…' : '正在切换到本地预览…', true);
    try {
      var data = await api('/api/preview/source', {
        method: 'POST',
        body: JSON.stringify({
          source: source,
          characterId: $('characterId') ? $('characterId').value : undefined,
          projectPath: $('projectPath') ? $('projectPath').value.trim() : undefined,
          previewPort: $('previewPort') ? $('previewPort').value : undefined,
        }),
      });
      if (data.status) fillForm(data.status);
      if (!data.ok) {
        showMsg(data.error || '切换失败', false);
        return;
      }
      setPreviewSourceUi(data.source || source);
      cloudPreviewRevision = Number((data.cloud && data.cloud.revision) || 0);
      if (data.preview) {
        $('previewFrame').dataset.url = '';
        renderPreview(data.preview);
      }
      if (previewSource === 'cloud') startCloudPreviewPoll();
      else stopCloudPreviewPoll();
      showMsg(
        data.message ||
          (data.pending
            ? '正在首次拉取云端，完成后自动预览…'
            : previewSource === 'cloud'
              ? '已切到云端预览'
              : '已切到本地预览'),
        true
      );
    } catch (e) {
      showMsg(String(e.message || e), false);
    } finally {
      setBusy(false);
    }
  }

  function showMsg(text, ok) {
    var el = $('msg');
    el.hidden = !text;
    el.textContent = text || '';
    el.className = 'msg ' + (ok ? 'ok' : 'err');
    var toast = $('fabToast');
    if (toast) {
      var dock = $('fabDock');
      var showToast = !!(text && dock && !dock.hidden);
      toast.hidden = !showToast;
      toast.textContent = text || '';
      toast.className = 'fab-toast' + (ok ? '' : ' is-err');
    }
  }

  function setBusy(busy, opts) {
    opts = opts || {};
    var keep = opts.keepEnabled || {};
    ['loginBtn', 'logoutBtn', 'logoutBtn2', 'logoutBtn3', 'logoutBtn4', 'loginCodeBtn', 'cookieLoginBtn', 'tgStartBtn', 'tgStopBtn', 'pingBtn', 'pullBtn', 'pullRetryBtn', 'previewStartBtn', 'previewStopBtn', 'previewReloadBtn', 'previewSrcLocal', 'previewSrcCloud', 'publishBtn', 'bridgeRefreshBtn', 'fabPublish', 'fabReload', 'fabSync', 'fabExpand', 'originProbeBtn', 'originSelect', 'agentReadyBtn', 'agentSendBtn', 'agentCancelBtn', 'agentToggleBtn', 'agentCollapseBtn', 'cardAiBtn', 'cardCopyPromptBtn', 'cardNewBtn2', 'cardSaveBtn', 'cardExportPngBtn', 'cardRefreshBtn', 'cardCloudBtn', 'cardReloadBtn', 'cardMenuBtn', 'cardVoiceRefreshBtn', 'cardVoiceClearBtn', 'cardPublishBtn', 'cardDraftBtn', 'cardCloudFilterAll', 'cardCloudFilterDraft', 'cardCloudFilterPub', 'cardCloudSearch'].forEach(function (id) {
      var el = $(id);
      if (!el) return;
      if (busy && keep[id]) {
        el.disabled = false;
        return;
      }
      el.disabled = !!busy;
    });
    // 文件选择不要 disabled：禁用会导致 label 点不开系统对话框
    ['cardAvatarFile', 'cardImageFile'].forEach(function (id) {
      var el = $(id);
      if (el) el.disabled = false;
    });
  }

  function reloadPreviewOnly() {
    var frame = $('previewFrame');
    if (!frame) return false;
    var src = frame.getAttribute('src') || '';
    if (!src || src === 'about:blank') {
      showMsg('预览未启动，请先点「启动预览」', false);
      return false;
    }
    try {
      // 同域时可直接 reload；跨端口也兜底改 src 带时间戳
      if (frame.contentWindow) {
        frame.contentWindow.location.reload();
        showMsg('已刷新预览', true);
        return true;
      }
    } catch (_) {}
    try {
      var u = new URL(src, window.location.href);
      u.searchParams.set('_ts', String(Date.now()));
      frame.dataset.url = u.origin + u.pathname + (u.searchParams.get('embed') ? '?embed=1' : '');
      frame.src = u.toString();
      showMsg('已刷新预览', true);
      return true;
    } catch (_) {
      frame.src = src;
      showMsg('已刷新预览', true);
      return true;
    }
  }

  function setFabProgress(percent, mode, counts) {
    var prog = $('fabProg');
    var fab = $('fabPublish');
    var label = $('fabLabel');
    if (!prog || !fab) return;
    var p = Math.max(0, Math.min(100, Number(percent) || 0));
    prog.style.strokeDasharray = String(FAB_CIRC);
    prog.style.strokeDashoffset = String(FAB_CIRC * (1 - p / 100));
    fab.classList.remove('is-ok', 'is-err', 'is-run');
    if (label) {
      label.classList.remove('is-count');
      label.style.fontSize = '';
      label.style.letterSpacing = '';
    }
    if (mode === 'ok') {
      fab.classList.add('is-ok');
      label.textContent = '完成';
    } else if (mode === 'err') {
      fab.classList.add('is-err');
      label.textContent = '失败';
    } else if (mode === 'run') {
      fab.classList.add('is-run');
      var total = counts && Number(counts.total) || 0;
      var cur = counts && Number(counts.current) || 0;
      if (total > 0 && label) {
        label.textContent = cur + '/' + total;
        label.classList.add('is-count');
        label.title = Math.round(p) + '%';
      } else if (label) {
        label.textContent = Math.round(p) + '%';
      }
    } else {
      label.textContent = '发布';
    }
  }

  function applyPublishJob(job) {
    job = job || {};
    var wrap = $('publishBarWrap');
    var bar = $('publishBar');
    var total = Number(job.total) || 0;
    var cur = Number(job.current != null ? job.current : job.synced) || 0;
    var pct = Number(job.percent) || 0;
    if (job.status === 'running' && total > 0) {
      pct = Math.max(pct, Math.round(38 + 59 * cur / total));
    }
    pct = Math.max(0, Math.min(100, pct));
    if (job.status === 'running') {
      if (wrap) wrap.hidden = false;
      if (bar) bar.style.width = Math.max(8, pct || 8) + '%';
      var msg = job.message || '发布中…';
      if (total > 0 && msg.indexOf(String(cur) + '/' + String(total)) < 0) {
        msg = '发布中 ' + cur + '/' + total + '（' + Math.round(pct) + '%）';
      } else if (total > 0 && msg.indexOf('%') < 0) {
        msg += '（' + Math.round(pct) + '%）';
      }
      if ($('publishMsg')) $('publishMsg').textContent = msg;
      setFabProgress(Math.max(8, pct || 8), 'run', { current: cur, total: total });
      return 'run';
    }
    if (job.status === 'ok') {
      if (wrap) wrap.hidden = false;
      if (bar) bar.style.width = '100%';
      if ($('publishMsg')) $('publishMsg').textContent = job.message || '发布成功';
      setFabProgress(100, 'ok');
      return 'ok';
    }
    if (job.status === 'error') {
      if (wrap) wrap.hidden = false;
      if (bar) bar.style.width = (pct || 100) + '%';
      if ($('publishMsg')) $('publishMsg').textContent = job.message || '发布失败';
      setFabProgress(pct || 100, 'err');
      return 'err';
    }
    return job.status || 'idle';
  }

  function isCardMode() {
    return !!( $('appShell') && $('appShell').classList.contains('is-card-mode') );
  }

  function setSidebarCollapsed(collapsed) {
    var app = $('appShell');
    var fab = $('fabDock');
    if (!app) return;
    if (collapsed) {
      app.classList.add('is-collapsed');
      // 角色卡全屏不显示游戏 FAB；游戏全屏才显示
      if (fab) fab.hidden = isCardMode();
      try { localStorage.setItem(SIDEBAR_KEY, '1'); } catch (_) {}
    } else {
      app.classList.remove('is-collapsed');
      if (fab) fab.hidden = true;
      try { localStorage.setItem(SIDEBAR_KEY, '0'); } catch (_) {}
      setFabProgress(0, 'idle');
    }
  }

  function setCardFullscreen(on) {
    var app = $('appShell');
    if (!app) return;
    if (on) {
      app.classList.add('is-card-mode');
      setSidebarCollapsed(true);
    } else {
      app.classList.remove('is-card-mode');
      var fab = $('fabDock');
      if (fab && isSidebarCollapsed()) fab.hidden = false;
    }
  }

  function isSidebarCollapsed() {
    return !!( $('appShell') && $('appShell').classList.contains('is-collapsed') );
  }

  function renderBridge(bridge, preview) {
    var card = $('bridgeCard');
    var alive = !!(preview && preview.running);
    card.className = 'bridge-card ' + (alive && bridge && bridge.loggedIn ? 'on' : 'off');
    $('bridgeLamp').className = 'dot';
    if (!alive) {
      $('bridgeHeadText').textContent = '预览未启动';
      $('bridgeProject').textContent = '—';
      $('bridgeContainer').textContent = '—';
      $('bridgePort').textContent = (preview && preview.port) || $('previewPort').value || '—';
      $('bridgeUser').textContent = '—';
      $('bridgeServices').textContent = '启动预览后显示云端接入状态';
      $('bridgeRemain').textContent = '—';
      return;
    }
    if (!bridge) {
      $('bridgeHeadText').textContent = '预览已启动 · 桥接读取中';
      $('bridgePort').textContent = preview.port || '—';
      return;
    }
    $('bridgeHeadText').textContent = bridge.loggedIn ? '已连接 DZMM 云端' : '预览已启动 · 未登录';
    $('bridgeProject').textContent = bridge.projectName || '—';
    $('bridgeContainer').textContent = bridge.containerShort || bridge.containerId || bridge.gameId || '—';
    $('bridgePort').textContent = bridge.port || preview.port || '—';
    $('bridgeUser').textContent = bridge.displayName || bridge.email || '—';
    $('bridgeServices').textContent = bridge.services || '服务端函数、生图、对话模型已接入';
    var remain = bridge.remainSec != null ? bridge.remainSec : bridge.remain;
    var mins = bridge.remainMin != null ? bridge.remainMin : Math.floor((remain || 0) / 60);
    $('bridgeRemain').textContent = '登录态剩余约 ' + mins + ' 分钟（' + (remain || 0) + 's）';
  }

  async function refreshBridge(force) {
    try {
      var data = await api(force ? '/api/bridge?force=1' : '/api/bridge');
      renderBridge(data.bridge, data.preview || {});
      return data;
    } catch (e) {
      renderBridge(null, { running: false });
      return null;
    }
  }

  function startBridgePoll() {
    stopBridgePoll();
    refreshBridge();
    bridgeTimer = setInterval(refreshBridge, 5000);
  }

  function stopBridgePoll() {
    if (bridgeTimer) {
      clearInterval(bridgeTimer);
      bridgeTimer = null;
    }
  }

  function stopPublishPoll() {
    if (publishTimer) {
      clearInterval(publishTimer);
      publishTimer = null;
    }
  }

  function startPublishPoll() {
    stopPublishPoll();
    publishTimer = setInterval(async function () {
      try {
        var data = await api('/api/bridge/publish');
        var state = applyPublishJob(data.job || {});
        if (state === 'ok') {
          showMsg('发布成功', true);
          stopPublishPoll();
          setBusy(false);
          setTimeout(function () { setFabProgress(0, 'idle'); }, 1600);
        } else if (state === 'err') {
          showMsg((data.job && data.job.message) || '发布失败', false);
          stopPublishPoll();
          setBusy(false);
          setTimeout(function () { setFabProgress(0, 'idle'); }, 2200);
        }
      } catch (_) {}
    }, 350);
  }

  async function runPublish(options) {
    var opts = options || {};
    var direct = !!opts.direct;
    if (!direct && !confirm('确认发布到线上玩家版？\n将把当前容器内容正式上线。')) return;
    setBusy(true);
    $('publishBarWrap').hidden = false;
    $('publishBar').style.width = '12%';
    $('publishMsg').textContent = '正在发布…';
    setFabProgress(12, 'run');
    showMsg('正在发布…', true);
    try {
      var data = await api('/api/bridge/publish', {
        method: 'POST',
        body: JSON.stringify({ message: 'publish from local console' }),
      });
      if (!data.ok) {
        showMsg(data.error || '发布失败', false);
        $('publishMsg').textContent = data.error || '发布失败';
        setFabProgress(100, 'err');
        setBusy(false);
        setTimeout(function () { setFabProgress(0, 'idle'); }, 2200);
        return;
      }
      if (data.via === 'studio') {
        $('publishBar').style.width = '100%';
        $('publishMsg').textContent = data.message || '发布成功';
        setFabProgress(100, 'ok');
        showMsg(data.message || '发布成功', true);
        setBusy(false);
        setTimeout(function () { setFabProgress(0, 'idle'); }, 1600);
        return;
      }
      startPublishPoll();
      if (data.job) applyPublishJob(data.job);
    } catch (e) {
      showMsg(String(e.message || e), false);
      setFabProgress(100, 'err');
      setBusy(false);
      setTimeout(function () { setFabProgress(0, 'idle'); }, 2200);
    }
  }

  function setStageLive(alive) {
    var wrap = document.querySelector('.stage-frame');
    if (!wrap) return;
    if (alive) wrap.classList.add('is-live');
    else wrap.classList.remove('is-live');
  }

  function renderPreview(preview, opts) {
    opts = opts || {};
    var meta = $('previewMeta');
    var link = $('previewLink');
    var frame = $('previewFrame');
    if (!opts.keepSourceUi && preview && (preview.desiredSource || preview.source)) {
      setPreviewSourceUi(preview.desiredSource || preview.source);
    }
    if (!preview) {
      meta.textContent = '预览未启动';
      link.href = '#';
      setStageLive(false);
      if (previewSource !== 'cloud') stopCloudPreviewPoll();
      return;
    }
    var url = preview.url || '';
    var alive = !!(preview.running && url);
    var actual = preview.source || 'local';
    var srcLabel = actual === 'cloud' ? '云端' : '本地';
    if (alive) {
      var cloud = preview.cloud || {};
      var extra = preview.projectPath ? ' · ' + preview.projectPath : '';
      if (actual === 'cloud') {
        extra = ' · rev ' + (cloud.revision || cloudPreviewRevision || 0) +
          (cloud.message ? ' · ' + cloud.message : '');
      }
      meta.textContent = srcLabel + '预览中 · ' + url + extra;
      link.href = url;
      setStageLive(true);
      var embed = previewEmbedUrl(url);
      if (frame.dataset.url !== embed) {
        frame.dataset.url = embed;
        frame.src = embed + (embed.indexOf('?') >= 0 ? '&' : '?') + '_ts=' + Date.now();
      }
      startBridgePoll();
      if (previewSource === 'cloud' || actual === 'cloud') startCloudPreviewPoll();
      else stopCloudPreviewPoll();
    } else {
      var cloud2 = preview.cloud || {};
      if (previewSource === 'cloud') {
        meta.textContent = cloud2.message || preview.message || preview.error || '正在拉取云端…';
        startCloudPreviewPoll();
      } else {
        meta.textContent = preview.message || preview.error || '预览未启动';
      }
      link.href = url || '#';
      if (!preview.running) {
        setStageLive(false);
        stopBridgePoll();
        if (previewSource !== 'cloud') stopCloudPreviewPoll();
        renderBridge(null, preview);
      }
      if (preview.error) {
        frame.dataset.url = '';
        frame.src = 'about:blank';
        setStageLive(false);
        stopBridgePoll();
        stopCloudPreviewPoll();
      }
    }
  }

  function setStageMode(mode) {
    var game = $('gameStage');
    var card = $('cardStage');
    if (!game || !card) return;
    var isCard = mode === 'card';
    if (isCard) {
      game.setAttribute('hidden', '');
      card.removeAttribute('hidden');
    } else {
      card.setAttribute('hidden', '');
      game.removeAttribute('hidden');
    }
  }

  /** 角色卡模式：卸掉游戏预览/桥接/同步 UI，只留登录态给写卡与试玩。 */
  function detachGameRuntime() {
    stopBridgePoll();
    stopCloudPreviewPoll();
    stopPublishPoll();
    var frame = $('previewFrame');
    if (frame) {
      frame.dataset.url = '';
      frame.src = 'about:blank';
    }
    setStageLive(false);
    renderBridge(null, { running: false, message: '角色卡模式' });
    var meta = $('previewMeta');
    if (meta) meta.textContent = '角色卡模式 · 游戏预览已停用';
    var link = $('previewLink');
    if (link) link.href = '#';
    var fab = $('fabDock');
    if (fab) fab.hidden = true;
  }

  function persistConsoleMode(mode) {
    return api('/api/console-mode', {
      method: 'POST',
      body: JSON.stringify({ mode: mode }),
    }).catch(function () { return null; });
  }

  function switchPanel(name, opts) {
    opts = opts || {};
    if (!name) return;
    if (name === 'card') {
      if (consoleMode !== 'card') {
        switchConsoleMode('card', { skipPanel: true });
      }
    } else if (consoleMode !== 'game') {
      switchConsoleMode('game', { skipPanel: true, panel: name });
    }

    document.querySelectorAll('.menu-item').forEach(function (btn) {
      btn.classList.toggle('active', btn.getAttribute('data-panel') === name);
    });
    document.querySelectorAll('.sidebar-scroll .panel').forEach(function (panel) {
      var match = panel.getAttribute('data-panel') === name;
      panel.classList.toggle('active', match);
      if (match) panel.removeAttribute('hidden');
      else panel.setAttribute('hidden', '');
    });

    if (name === 'card') {
      setStageMode('card');
      // 切到角色卡：侧栏直接是本地写卡；打开具体卡时再全屏
      if (opts.fullscreen) setCardFullscreen(true);
      else setCardFullscreen(false);
      startCardListPoll();
      if ($('cardBrief') && $('cardBriefMain') && !$('cardBriefMain').value) {
        $('cardBriefMain').value = $('cardBrief').value || '';
      }
      if (currentCardId) startCardPoll();
    } else {
      lastGamePanel = name;
      stopCardPoll();
      stopCardListPoll();
      setStageMode('game');
      setCardFullscreen(false);
    }
  }

  function switchConsoleMode(mode, opts) {
    opts = opts || {};
    mode = mode === 'card' ? 'card' : 'game';
    var prev = consoleMode;
    consoleMode = mode;
    try { localStorage.setItem(CONSOLE_MODE_KEY, mode); } catch (_) {}

    document.querySelectorAll('.mode-item').forEach(function (btn) {
      btn.classList.toggle('active', btn.getAttribute('data-mode') === mode);
    });
    var sidebar = $('sidebar');
    if (sidebar) sidebar.classList.toggle('is-card-console', mode === 'card');
    if ($('agentToggleBtn')) $('agentToggleBtn').hidden = mode === 'card';

    if (mode === 'card') {
      detachGameRuntime();
      if (prev !== 'card' || opts.forcePersist) {
        persistConsoleMode('card').then(function (data) {
          if (data && data.status) fillForm(data.status, { skipPreview: true });
        });
      }
    } else if (prev === 'card' || opts.forcePersist) {
      persistConsoleMode('game');
    }

    if (opts.skipPanel) return;

    if (mode === 'card') {
      switchPanel('card', { fullscreen: false });
    } else {
      switchPanel(opts.panel || lastGamePanel || 'login');
    }
  }

  function tagsToText(tags) {
    if (!Array.isArray(tags)) return '';
    return tags.map(function (t) { return String(t || '').trim(); }).filter(Boolean).join(', ');
  }

  function textToTags(text) {
    return String(text || '')
      .split(/[,，]/)
      .map(function (t) { return t.trim(); })
      .filter(Boolean);
  }

  function cardAssetUrl(localId, url) {
    var u = String(url || '').trim();
    if (!u) return '';
    if (/^https?:\/\//i.test(u) || u.indexOf('/api/card/asset') === 0 || u.indexOf('data:') === 0) {
      return u;
    }
    // 本地相对路径：assets/avatar.png
    var rel = u.replace(/^local:\/\//, '');
    if (!localId) return '';
    return '/api/card/asset?id=' + encodeURIComponent(localId) +
      '&path=' + encodeURIComponent(rel) +
      '&t=' + encodeURIComponent(String(currentCardMtime || Date.now()));
  }

  function updateAvatarPreview(url) {
    var img = $('cardAvatarPreview');
    var empty = $('cardAvatarEmpty');
    if (!img) return;
    var src = cardAssetUrl(currentCardId, url || ($('cardAvatarUrl') && $('cardAvatarUrl').value) || '');
    if (!src) {
      img.removeAttribute('src');
      img.hidden = true;
      if (empty) empty.hidden = false;
      return;
    }
    img.src = src;
    img.hidden = false;
    if (empty) empty.hidden = true;
  }

  function renderImageGallery(images) {
    var box = $('cardImageGallery');
    if (!box) return;
    box.innerHTML = '';
    (images || []).forEach(function (it, idx) {
      var tile = document.createElement('div');
      tile.className = 'image-tile';
      var img = document.createElement('img');
      img.alt = it.name || ('立绘' + (idx + 1));
      img.src = cardAssetUrl(currentCardId, it.url || '');
      var name = document.createElement('div');
      name.className = 'image-tile-name';
      name.textContent = it.name || it.url || ('#' + (idx + 1));
      var del = document.createElement('button');
      del.type = 'button';
      del.className = 'btn';
      del.textContent = '删除';
      del.addEventListener('click', function () { removeCardImage(idx); });
      tile.appendChild(img);
      tile.appendChild(name);
      tile.appendChild(del);
      box.appendChild(tile);
    });
    if (!images || !images.length) {
      box.innerHTML = '<p class="hint">还没有立绘，点上面导入</p>';
    }
  }

  function renderVoiceSelected(vs) {
    var el = $('cardVoiceSelected');
    if (!el) return;
    if (!vs || !vs.voice) {
      el.textContent = '未选择音色';
      return;
    }
    var v = vs.voice;
    el.textContent = '已选 · ' + (v.name || v.id) + (v.gender ? ' · ' + v.gender : '');
  }

  function filteredVoices() {
    var q = (($('cardVoiceSearch') && $('cardVoiceSearch').value) || '').trim().toLowerCase();
    var gender = ($('cardVoiceGender') && $('cardVoiceGender').value) || 'all';
    var all = [].concat(voiceCatalog.mine || [], voiceCatalog.public || []);
    return all.filter(function (v) {
      if (gender !== 'all' && String(v.gender || '') !== gender) return false;
      if (!q) return true;
      var blob = ((v.name || '') + ' ' + (v.description || '')).toLowerCase();
      return blob.indexOf(q) >= 0;
    });
  }

  function renderVoiceList() {
    var box = $('cardVoiceList');
    if (!box) return;
    box.innerHTML = '';
    var selectedId = '';
    try {
      var vs = safeJsonParse($('cardVoiceSettings') && $('cardVoiceSettings').value, null);
      selectedId = (vs && vs.voice && vs.voice.id) || '';
    } catch (_) {}
    var items = filteredVoices().slice(0, 80);
    if (!items.length) {
      box.innerHTML = '<p class="hint">无匹配音色（先点刷新，或放宽筛选）</p>';
      return;
    }
    items.forEach(function (v) {
      var card = document.createElement('div');
      card.className = 'voice-card' + (v.id === selectedId ? ' is-selected' : '');
      var av = document.createElement(v.avatarUrl ? 'img' : 'div');
      if (v.avatarUrl) {
        av.src = v.avatarUrl;
        av.alt = v.name || '';
      } else {
        av.className = 'voice-fallback';
        av.textContent = String(v.name || '?').charAt(0);
      }
      var meta = document.createElement('div');
      meta.className = 'voice-meta';
      meta.innerHTML =
        '<p class="voice-name"></p><p class="voice-sub"></p><div class="voice-actions"></div>';
      meta.querySelector('.voice-name').textContent = (v.mine ? '[我的] ' : '') + (v.name || v.id);
      meta.querySelector('.voice-sub').textContent =
        (v.gender || '未知') + (v.description ? ' · ' + v.description : '');
      var actions = meta.querySelector('.voice-actions');
      var playBtn = document.createElement('button');
      playBtn.type = 'button';
      playBtn.className = 'btn';
      playBtn.textContent = '试听';
      playBtn.disabled = !v.previewUrl;
      playBtn.addEventListener('click', function (ev) {
        ev.stopPropagation();
        if (!v.previewUrl) return;
        try {
          if (voiceAudio) { voiceAudio.pause(); }
          voiceAudio = new Audio(v.previewUrl);
          voiceAudio.play();
        } catch (_) {}
      });
      var pickBtn = document.createElement('button');
      pickBtn.type = 'button';
      pickBtn.className = 'btn accent';
      pickBtn.textContent = '选用';
      pickBtn.addEventListener('click', function (ev) {
        ev.stopPropagation();
        selectVoice(v);
      });
      actions.appendChild(playBtn);
      actions.appendChild(pickBtn);
      card.appendChild(av);
      card.appendChild(meta);
      box.appendChild(card);
    });
  }

  function safeJsonParse(text, fallback) {
    var raw = String(text || '').trim();
    if (!raw) return fallback;
    try {
      return JSON.parse(raw);
    } catch (_) {
      return fallback;
    }
  }

  var ZOOM_ICON_SVG =
    '<svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">' +
    '<circle cx="10.5" cy="10.5" r="6.25" fill="none" stroke="currentColor" stroke-width="2"/>' +
    '<path d="M15.2 15.2L20 20" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>' +
    '<path d="M10.5 8v5M8 10.5h5" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/>' +
    '</svg>';

  function paintZoomButtons(root) {
    (root || document).querySelectorAll('.field-zoom-btn').forEach(function (btn) {
      if (!btn.querySelector('svg')) btn.innerHTML = ZOOM_ICON_SVG;
    });
  }

  function closeTextZoom() {
    var modal = $('textZoomModal');
    if (modal) modal.hidden = true;
  }

  function openTextZoom(title, text) {
    var modal = $('textZoomModal');
    var titleEl = $('textZoomTitle');
    var body = $('textZoomBody');
    if (!modal || !titleEl || !body) return;
    titleEl.textContent = title || '全文预览';
    var raw = String(text == null ? '' : text);
    body.textContent = raw.trim() ? raw : '（空）';
    modal.hidden = false;
    body.scrollTop = 0;
  }

  function resolveZoomSource(btn) {
    if (!btn) return null;
    var targetId = btn.getAttribute('data-zoom-target');
    if (targetId && $(targetId)) {
      var el = $(targetId);
      var label = btn.closest('label');
      var span = label && label.querySelector(':scope > span');
      return {
        title: (span && span.textContent.trim()) || targetId,
        text: el.value || '',
      };
    }
    var box = btn.closest('.field-zoom-box');
    var ta = box && box.querySelector('textarea');
    if (!ta) return null;
    var lab = btn.closest('label');
    var head = lab && lab.querySelector(':scope > span');
    var entry = btn.closest('.wb-entry');
    var nameInput = entry && entry.querySelector('.wb-name');
    var entryName = nameInput && nameInput.value.trim();
    var base = (head && head.textContent.trim()) || '正文';
    return {
      title: entryName ? (base + ' · ' + entryName) : base,
      text: ta.value || '',
    };
  }

  function bindTextZoomUi() {
    paintZoomButtons(document);
    document.addEventListener('click', function (ev) {
      var btn = ev.target && ev.target.closest && ev.target.closest('.field-zoom-btn');
      if (btn) {
        ev.preventDefault();
        var src = resolveZoomSource(btn);
        if (src) openTextZoom(src.title, src.text);
        return;
      }
      if (ev.target && ev.target.id === 'textZoomModal') {
        closeTextZoom();
      }
    });
    if ($('textZoomCloseBtn')) {
      $('textZoomCloseBtn').addEventListener('click', closeTextZoom);
    }
    document.addEventListener('keydown', function (ev) {
      if (ev.key === 'Escape' && $('textZoomModal') && !$('textZoomModal').hidden) {
        closeTextZoom();
      }
    });
  }

  function renderWorldBookEntries(entries) {
    var box = $('cardWbEntries');
    if (!box) return;
    box.innerHTML = '';
    (entries || []).forEach(function (ent, idx) {
      var wrap = document.createElement('div');
      wrap.className = 'wb-entry';
      wrap.dataset.idx = String(idx);
      wrap.dataset.entryId = String(ent.id != null ? ent.id : (idx + 1));
      wrap.dataset.position = String(ent.position != null ? ent.position : 4);
      wrap.dataset.priority = String(ent.priority != null ? ent.priority : 100);
      wrap.dataset.insertionOrder = String(ent.insertion_order != null ? ent.insertion_order : idx);
      try {
        wrap.dataset.extensions = JSON.stringify(ent.extensions && typeof ent.extensions === 'object' ? ent.extensions : {});
      } catch (_) {
        wrap.dataset.extensions = '{}';
      }
      wrap.innerHTML =
        '<div class="wb-entry-head"><strong>条目 ' + (idx + 1) + '</strong>' +
        '<button type="button" class="btn wb-del" data-idx="' + idx + '">删除</button></div>' +
        '<label><span>标题 name</span><input class="wb-name" type="text"></label>' +
        '<label><span>关键词 keys（逗号分隔；常驻可空）</span><input class="wb-keys" type="text"></label>' +
        '<label class="field-zoom"><span>正文 content</span>' +
        '<div class="field-zoom-box">' +
        '<button type="button" class="field-zoom-btn" title="预览全文" aria-label="预览全文"></button>' +
        '<textarea class="wb-content" rows="4"></textarea></div></label>' +
        '<div class="wb-checks">' +
        '<label><input class="wb-enabled" type="checkbox"> 启用</label>' +
        '<label><input class="wb-constant" type="checkbox"> 常驻 constant</label>' +
        '</div>' +
        '<label><span>备注 comment</span><input class="wb-comment" type="text"></label>';
      box.appendChild(wrap);
      wrap.querySelector('.wb-name').value = ent.name || '';
      wrap.querySelector('.wb-keys').value = tagsToText(ent.keys || []);
      wrap.querySelector('.wb-content').value = ent.content || '';
      wrap.querySelector('.wb-enabled').checked = ent.enabled !== false;
      wrap.querySelector('.wb-constant').checked = !!ent.constant;
      wrap.querySelector('.wb-comment').value = (ent.extensions && ent.extensions.comment) || '';
      wrap.querySelector('.wb-del').addEventListener('click', function () {
        var list = collectWorldBookEntries();
        list.splice(idx, 1);
        renderWorldBookEntries(list);
        updateCardPreview();
      });
      wrap.querySelectorAll('input, textarea').forEach(function (field) {
        field.addEventListener('input', updateCardPreview);
        field.addEventListener('change', updateCardPreview);
      });
      paintZoomButtons(wrap);
    });
  }

  function collectWorldBookEntries() {
    var box = $('cardWbEntries');
    if (!box) return [];
    var out = [];
    box.querySelectorAll('.wb-entry').forEach(function (wrap, idx) {
      var comment = (wrap.querySelector('.wb-comment').value || '').trim();
      var ext = {};
      try {
        ext = JSON.parse(wrap.dataset.extensions || '{}') || {};
      } catch (_) {
        ext = {};
      }
      if (comment) ext.comment = comment;
      else delete ext.comment;
      var id = parseInt(wrap.dataset.entryId, 10);
      var position = parseInt(wrap.dataset.position, 10);
      var priority = parseInt(wrap.dataset.priority, 10);
      var insertionOrder = parseInt(wrap.dataset.insertionOrder, 10);
      out.push({
        id: Number.isFinite(id) ? id : (idx + 1),
        name: wrap.querySelector('.wb-name').value.trim() || ('条目' + (idx + 1)),
        keys: textToTags(wrap.querySelector('.wb-keys').value),
        content: wrap.querySelector('.wb-content').value,
        enabled: !!wrap.querySelector('.wb-enabled').checked,
        constant: !!wrap.querySelector('.wb-constant').checked,
        insertion_order: Number.isFinite(insertionOrder) ? insertionOrder : idx,
        position: Number.isFinite(position) ? position : 4,
        priority: Number.isFinite(priority) ? priority : 100,
        extensions: ext,
      });
    });
    return out;
  }

  function rememberDiskField(id, value) {
    lastDiskFieldValues[id] = value == null ? '' : String(value);
  }

  function setInputFromDisk(id, value, opts) {
    opts = opts || {};
    var el = $(id);
    if (!el) return false;
    var next = value == null ? '' : String(value);
    var ae = document.activeElement;
    var prevDisk = Object.prototype.hasOwnProperty.call(lastDiskFieldValues, id)
      ? lastDiskFieldValues[id]
      : null;
    // 网页里正在改这个框（与上次磁盘快照不一致）时不覆盖；只看盘时即使聚焦也热更新
    if (ae === el && prevDisk !== null && el.value !== prevDisk && el.value !== next) {
      return false;
    }
    if (el.value === next) {
      rememberDiskField(id, next);
      return false;
    }
    var stickBottom = false;
    if (el.tagName === 'TEXTAREA') {
      stickBottom = (el.scrollHeight - el.scrollTop - el.clientHeight) < 48;
    }
    el.value = next;
    rememberDiskField(id, next);
    if (stickBottom) el.scrollTop = el.scrollHeight;
    if (opts.forceCaretEnd && ae === el && typeof el.setSelectionRange === 'function') {
      try {
        var n = next.length;
        el.setSelectionRange(n, n);
      } catch (_) {}
    }
    return true;
  }

  function fillCardForm(card, localId, opts) {
    opts = opts || {};
    var live = !!opts.live;
    var nextId = localId || (card && card._meta && card._meta.localId) || '';
    var idChanged = !!(nextId && nextId !== currentCardId);
    if (idChanged) {
      lastDiskFieldValues = Object.create(null);
      currentCardSig = '';
      stopCardPoll();
    }
    cardApplyingRemote = true;
    currentCard = card || null;
    currentCardId = nextId;
    currentCardFolder = (card && card._meta && card._meta.folder) || currentCardFolder || '';
    try {
      if (currentCardId) localStorage.setItem(CONSOLE_CARD_KEY, currentCardId);
    } catch (_) {}
    if (typeof opts.mtime === 'number') currentCardMtime = opts.mtime;
    else if (card && card._meta && card._meta.mtime) currentCardMtime = Number(card._meta.mtime) || currentCardMtime;
    if (typeof opts.sig === 'string') currentCardSig = opts.sig;
    var d = (card && card.data) || {};
    var brief = (card && card._meta && card._meta.brief) || '';
    var book = d.character_book || {};
    var tagsText = tagsToText(d.tags);
    var repliesText = Array.isArray(d.suggested_replies) ? d.suggested_replies.join('\n') : '';
    var chatText = JSON.stringify(d.chat_history || [], null, 2);
    var imageText = JSON.stringify(d.image_info || [], null, 2);
    var voiceText = d.voice_settings == null ? '' : JSON.stringify(d.voice_settings, null, 2);
    var touched = false;
    touched = setInputFromDisk('cardName', d.name || '') || touched;
    touched = setInputFromDisk('cardDescription', d.description || '') || touched;
    touched = setInputFromDisk('cardPersonality', d.personality || '') || touched;
    touched = setInputFromDisk('cardScenario', d.scenario || '') || touched;
    touched = setInputFromDisk('cardSystemPrompt', d.system_prompt || '') || touched;
    touched = setInputFromDisk('cardFirstMes', d.first_mes || '', { forceCaretEnd: live }) || touched;
    touched = setInputFromDisk('cardTags', tagsText) || touched;
    touched = setInputFromDisk('cardNotes', d.creator_notes || '') || touched;
    touched = setInputFromDisk('cardCreator', d.creator || '') || touched;
    touched = setInputFromDisk('cardVersion', d.character_version || '') || touched;
    touched = setInputFromDisk('cardBookName', book.name || '世界设定') || touched;
    touched = setInputFromDisk('cardSuggestedReplies', repliesText) || touched;
    touched = setInputFromDisk('cardChatHistory', chatText) || touched;
    touched = setInputFromDisk('cardAvatarUrl', d.avatar_url || '') || touched;
    touched = setInputFromDisk('cardImageInfo', imageText) || touched;
    touched = setInputFromDisk('cardVoiceSettings', voiceText) || touched;
    touched = setInputFromDisk('cardBriefMain', brief) || touched;
    touched = setInputFromDisk('cardBrief', brief) || touched;
    if ($('cardFolderName') && currentCardId) {
      touched = setInputFromDisk('cardFolderName', currentCardId) || touched;
    }

    var ae = document.activeElement;
    var focusInWb = !!(ae && $('cardWbEntries') && $('cardWbEntries').contains(ae));
    if (!live || !focusInWb) {
      renderWorldBookEntries(Array.isArray(book.entries) ? book.entries : []);
    }
    if (!live || (ae !== $('cardAvatarUrl'))) {
      updateAvatarPreview(d.avatar_url || '');
    }
    if (!live || (ae !== $('cardImageInfo'))) {
      renderImageGallery(Array.isArray(d.image_info) ? d.image_info : []);
    }
    if (!live || (ae !== $('cardVoiceSettings'))) {
      renderVoiceSelected(d.voice_settings || null);
      renderVoiceList();
    }

    var wbCount = (book.entries && book.entries.length) || 0;
    var metaBase = (currentCardFolder || ('卡/' + currentCardId)) + ' · 世界书 ' + wbCount + ' 条';
    $('cardStageMeta').textContent = live && touched ? (metaBase + ' · 实时同步') : metaBase;
    updateCardPreview();
    cardApplyingRemote = false;
    if (currentCardId) startCardPoll();
    updatePlayGate();
    // 换卡时退出试玩会话
    if (cardPlayMode && playState.cardId && playState.cardId !== currentCloudCardId()) {
      void exitCardPlay({ keepPanel: false, deleteChat: true });
    }
  }

  function collectCardFromForm() {
    var base = currentCard && typeof currentCard === 'object'
      ? JSON.parse(JSON.stringify(currentCard))
      : {
          spec: 'chara_card_v3',
          spec_version: '3.0',
          data: {},
          _meta: { source: 'local-folder' },
        };
    if (!base.data || typeof base.data !== 'object') base.data = {};
    if (!base._meta || typeof base._meta !== 'object') base._meta = {};
    base.data.name = $('cardName').value.trim() || ($('cardFolderName') && $('cardFolderName').value.trim()) || '未命名角色';
    base.data.description = $('cardDescription').value;
    base.data.personality = $('cardPersonality').value;
    base.data.scenario = $('cardScenario').value;
    base.data.system_prompt = ($('cardSystemPrompt') && $('cardSystemPrompt').value) || '';
    base.data.first_mes = $('cardFirstMes').value;
    base.data.tags = textToTags($('cardTags').value);
    base.data.creator_notes = $('cardNotes').value;
    base.data.creator = ($('cardCreator') && $('cardCreator').value) || '';
    base.data.character_version = ($('cardVersion') && $('cardVersion').value) || '';
    base.data.character_book = {
      name: ($('cardBookName') && $('cardBookName').value.trim()) || '世界设定',
      entries: collectWorldBookEntries(),
      extensions: (base.data.character_book && base.data.character_book.extensions) || {},
    };
    base.data.suggested_replies = ($('cardSuggestedReplies') && $('cardSuggestedReplies').value || '')
      .split(/\r?\n/).map(function (s) { return s.trim(); }).filter(Boolean);
    base.data.chat_history = safeJsonParse($('cardChatHistory') && $('cardChatHistory').value, base.data.chat_history || []);
    base.data.avatar_url = ($('cardAvatarUrl') && $('cardAvatarUrl').value.trim()) || '';
    base.data.image_info = safeJsonParse($('cardImageInfo') && $('cardImageInfo').value, base.data.image_info || []);
    var voiceRaw = ($('cardVoiceSettings') && $('cardVoiceSettings').value || '').trim();
    base.data.voice_settings = voiceRaw ? safeJsonParse(voiceRaw, base.data.voice_settings || null) : null;
    base._meta.brief = getCardBrief();
    base.spec = base.spec || 'chara_card_v3';
    base.spec_version = base.spec_version || '3.0';
    return base;
  }

  function stopCardPoll() {
    if (cardPollTimer) {
      clearInterval(cardPollTimer);
      cardPollTimer = null;
    }
    cardPollBusy = false;
  }

  async function pollCardOnce() {
    if (!currentCardId || !isCardMode() || cardPlayMode || cardPollBusy) return;
    cardPollBusy = true;
    try {
      var data = await api(
        '/api/card/poll?id=' + encodeURIComponent(currentCardId) +
        '&since=' + encodeURIComponent(String(currentCardMtime || 0)) +
        '&sig=' + encodeURIComponent(String(currentCardSig || ''))
      );
      if (!data.ok || !data.changed || !data.card) {
        if (data && data.ok) {
          if (typeof data.mtime === 'number') currentCardMtime = data.mtime;
          if (typeof data.sig === 'string') currentCardSig = data.sig;
        }
        return;
      }
      fillCardForm(data.card, data.localId || currentCardId, {
        mtime: data.mtime || 0,
        sig: data.sig || '',
        live: true,
      });
      currentCardFolder = data.folder || currentCardFolder;
      setCardMsg('实时同步 · 卡/' + (data.localId || currentCardId), true);
    } catch (_) {
      // 静默：轮询失败不打断编辑
    } finally {
      cardPollBusy = false;
    }
  }

  function startCardPoll() {
    if (!currentCardId) {
      stopCardPoll();
      return;
    }
    if (cardPollTimer) return; // 已在跑，避免 fill 时反复重启丢掉节拍
    cardPollTimer = setInterval(function () {
      void pollCardOnce();
    }, CARD_POLL_MS);
    void pollCardOnce();
  }

  function stopCardListPoll() {
    if (cardListPollTimer) {
      clearInterval(cardListPollTimer);
      cardListPollTimer = null;
    }
    cardListPollBusy = false;
  }

  function listItemsSig(items, kind) {
    return (items || []).map(function (it) {
      if (kind === 'cloud') {
        return [
          it.cloudId, it.isDraft ? 1 : 0, it.name || '',
          it.isListed ? 1 : 0, it.isPendingReview ? 1 : 0,
          it.publishStatus || '', it.dbId || '', it.updatedAt || it.createdAt || ''
        ].join('\t');
      }
      return [
        it.localId, it.mtime || 0, it.updatedAt || '',
        it.cloudId || '', it.draftId || '',
        it.isListed ? 1 : 0, it.isPendingReview ? 1 : 0, it.publishStatus || '',
        it.name || ''
      ].join('\t');
    }).join('|');
  }

  async function tickCardLists() {
    if (consoleMode !== 'card' || cardListPollBusy) return;
    cardListPollBusy = true;
    cardListPollTick += 1;
    try {
      await refreshCardList({ quiet: true });
      // 多数轮询走轻量云端列表；每 4 次补一次上架状态
      var quick = (cardListPollTick % 4) !== 0;
      await refreshCloudCardList(false, { quiet: true, quick: quick, skipLocal: true });
    } catch (_) {
      // 静默：登录失效时不刷错误打扰编辑
    } finally {
      cardListPollBusy = false;
    }
  }

  function startCardListPoll() {
    stopCardListPoll();
    lastLocalListSig = '';
    lastCloudListSig = '';
    cardListPollTick = 0;
    tickCardLists();
    cardListPollTimer = setInterval(tickCardLists, 2500);
  }

  var _tokenEncoder = typeof TextEncoder !== 'undefined' ? new TextEncoder() : null;

  /** SillyTavern / GPT 常见估算：UTF-8 字节长度 ÷ 4 */
  function estimateTokens(text) {
    var s = text == null ? '' : String(text);
    if (!s) return 0;
    var bytes = _tokenEncoder ? _tokenEncoder.encode(s).length : unescape(encodeURIComponent(s)).length;
    return Math.ceil(bytes / 4);
  }

  function joinTokenParts(parts) {
    return (parts || []).filter(function (p) { return p != null && String(p).length; }).join('\n');
  }

  function worldBookEntryText(ent) {
    if (!ent || ent.enabled === false) return '';
    var keys = Array.isArray(ent.keys) ? ent.keys.join(', ') : '';
    return joinTokenParts([ent.name || '', keys, ent.content || '']);
  }

  function chatHistoryPlain(chat) {
    if (!Array.isArray(chat)) return '';
    var chunks = [];
    chat.forEach(function (seg) {
      if (!seg || typeof seg !== 'object') return;
      var msgs = seg.messages;
      if (!Array.isArray(msgs)) return;
      msgs.forEach(function (m) {
        if (m && m.content) chunks.push(String(m.content));
      });
    });
    return chunks.join('\n');
  }

  /**
   * 卡定义 token + 常规聊天发送 token（估算）。
   * 常驻发送 ≈ 每轮都会带上的人设；首轮 ≈ 常驻 + 开场白/开场对话 + 条件世界书全触发上限。
   */
  function computeCardTokenStats(card) {
    var d = (card && card.data) || {};
    var book = d.character_book && typeof d.character_book === 'object' ? d.character_book : {};
    var entries = Array.isArray(book.entries) ? book.entries : [];
    var enabled = entries.filter(function (e) { return e && e.enabled !== false; });
    var constant = enabled.filter(function (e) { return !!e.constant; });
    var conditional = enabled.filter(function (e) { return !e.constant; });

    var fields = {
      name: d.name || '',
      description: d.description || '',
      personality: d.personality || '',
      scenario: d.scenario || '',
      system_prompt: d.system_prompt || '',
      first_mes: d.first_mes || '',
      creator_notes: d.creator_notes || '',
      tags: Array.isArray(d.tags) ? d.tags.join(', ') : '',
      bookName: book.name || '',
      suggested: Array.isArray(d.suggested_replies) ? d.suggested_replies.join('\n') : '',
      chatHistory: chatHistoryPlain(d.chat_history),
      wbConstant: constant.map(worldBookEntryText).join('\n'),
      wbConditional: conditional.map(worldBookEntryText).join('\n'),
      wbAll: enabled.map(worldBookEntryText).join('\n'),
    };

    var tok = {};
    Object.keys(fields).forEach(function (k) { tok[k] = estimateTokens(fields[k]); });

    var cardDef = tok.name + tok.description + tok.personality + tok.scenario +
      tok.system_prompt + tok.first_mes + tok.creator_notes + tok.tags +
      tok.bookName + tok.suggested + tok.chatHistory + tok.wbAll;

    // 常规聊天每轮常驻：名称 + 简介/性格/场景/系统指令 + 常驻世界书
    var chatPermanent = tok.name + tok.description + tok.personality + tok.scenario +
      tok.system_prompt + tok.wbConstant;

    // 首轮额外：开场白（若对话流为空则用 first_mes）+ 开场对话 + 条件世界书全触发上限
    var opening = tok.chatHistory > 0 ? tok.chatHistory : tok.first_mes;
    var chatFirstRound = chatPermanent + opening + tok.wbConditional;

    return {
      cardDef: cardDef,
      chatPermanent: chatPermanent,
      chatFirstRound: chatFirstRound,
      opening: opening,
      wbConstant: tok.wbConstant,
      wbConditional: tok.wbConditional,
      firstMes: tok.first_mes,
      chatHistory: tok.chatHistory,
      breakdown: {
        description: tok.description,
        personality: tok.personality,
        scenario: tok.scenario,
        system_prompt: tok.system_prompt,
        first_mes: tok.first_mes,
        worldbook_constant: tok.wbConstant,
        worldbook_conditional: tok.wbConditional,
      },
      wbEnabled: enabled.length,
      wbConstantCount: constant.length,
      wbConditionalCount: conditional.length,
    };
  }

  function formatTok(n) {
    n = Math.max(0, Math.round(Number(n) || 0));
    return String(n).replace(/\B(?=(\d{3})+(?!\d))/g, ',');
  }

  function tokPairHtml(label, num, en) {
    return '<span class="tok-label' + (en ? ' tok-en' : '') + '">' + label + '</span>' +
      '<span class="tok-num">' + formatTok(num) + '</span>';
  }

  function updateCardTokenStats() {
    var summary = $('cardTokenSummary');
    if (!summary) return;
    var card;
    try {
      card = collectCardFromForm();
    } catch (_) {
      summary.innerHTML = '<span class="tok-label tok-en">Token</span><span class="tok-num">—</span>';
      summary.removeAttribute('title');
      summary.classList.remove('warn', 'hot');
      return;
    }
    var st = computeCardTokenStats(card);
    summary.innerHTML =
      tokPairHtml('Token', st.cardDef, true) +
      '<span class="tok-sep">·</span>' +
      tokPairHtml('常驻', st.chatPermanent, false) +
      '<span class="tok-sep">·</span>' +
      tokPairHtml('首轮', st.chatFirstRound, false);
    summary.classList.remove('warn', 'hot');
    if (st.chatPermanent >= 3000 || st.cardDef >= 4000) summary.classList.add('hot');
    else if (st.chatPermanent >= 1800 || st.cardDef >= 2500) summary.classList.add('warn');

    var b = st.breakdown;
    summary.title =
      '卡定义 ' + formatTok(st.cardDef) +
      ' · 常驻发送 ' + formatTok(st.chatPermanent) +
      ' · 首轮约 ' + formatTok(st.chatFirstRound) +
      '\n简介' + formatTok(b.description) +
      ' / 性格' + formatTok(b.personality) +
      ' / 场景' + formatTok(b.scenario) +
      ' / 系统' + formatTok(b.system_prompt) +
      ' · 开场' + formatTok(st.opening) +
      ' · 世界书常驻' + formatTok(st.wbConstant) +
      '（' + st.wbConstantCount + '）' +
      ' / 条件≤' + formatTok(st.wbConditional) +
      '（' + st.wbConditionalCount + '）' +
      '\n估算 UTF-8÷4；卡定义=卡内全文；常驻≈人设+常驻世界书；首轮≈常驻+开场+条件世界书上限';
  }

  function updateCardPreview() {
    var name = $('cardName').value.trim() || '—';
    if ($('cardPreviewName')) $('cardPreviewName').textContent = name;
    var wb = collectWorldBookEntries();
    var parts = [];
    parts.push('分类对齐平台：基础 / 世界书 / 对话 / 图片音色');
    if ($('cardTags').value.trim()) parts.push('标签：' + $('cardTags').value.trim());
    parts.push('世界书条目：' + wb.length);
    if ($('cardSystemPrompt') && $('cardSystemPrompt').value.trim()) parts.push('已填系统指令');
    if ($('cardFirstMes').value.trim()) parts.push('已填开场白');
    var box = $('cardLivePreview');
    if (box) box.hidden = false;
    if ($('cardPreviewBody')) $('cardPreviewBody').textContent = parts.join('\n');
    updateCardTokenStats();
  }

  function setCardMsg(text, ok) {
    var el = $('cardMsg');
    if (!el) return;
    el.textContent = text || '';
    el.style.color = ok === false ? 'var(--bad)' : '';
    if (text) el.removeAttribute('hidden');
    else el.setAttribute('hidden', '');
  }

  async function deleteLocalCard(localId) {
    if (!localId) return;
    if (!window.confirm('确定删除本地卡夹「' + localId + '」？此操作不可恢复。')) return;
    setBusy(true);
    try {
      var data = await api('/api/card/delete', {
        method: 'POST',
        body: JSON.stringify({ localId: localId }),
      });
      if (!data.ok) {
        showMsg(data.error || '删除失败', false);
        return;
      }
      if (currentCardId === localId) {
        currentCardId = '';
        currentCard = null;
        currentCardFolder = '';
        currentCardMtime = 0;
        currentCardSig = '';
        lastDiskFieldValues = Object.create(null);
        stopCardPoll();
        try { localStorage.removeItem(CONSOLE_CARD_KEY); } catch (_) {}
        if ($('cardStageMeta')) $('cardStageMeta').textContent = '未打开卡';
        if ($('cardTokenSummary')) {
          $('cardTokenSummary').innerHTML =
            '<span class="tok-label tok-en">Token</span><span class="tok-num">—</span>';
          $('cardTokenSummary').classList.remove('warn', 'hot');
        }
      }
      await refreshCardList();
      showMsg('已删除本地卡 · ' + localId, true);
    } catch (e) {
      showMsg(String(e.message || e), false);
    } finally {
      setBusy(false);
    }
  }

  async function deleteCloudCard(it) {
    if (!it || !it.cloudId) return;
    if (!it.isDraft && it.isListed) {
      showMsg('已上架卡请到官网下架/处理，控制台不可下架', false);
      return;
    }
    var linkedId = it.characterId || it.dbId || null;
    var tip;
    var alsoHide;
    if (it.isDraft) {
      tip = '确定删除云端草稿「' + (it.name || it.cloudId) + '」？';
      alsoHide = false;
    } else {
      tip = '隐藏已保存卡「' + (it.name || it.cloudId) + '」？\n等同删除：公开页将不可访问，并清理同卡草稿。';
      alsoHide = true;
    }
    if (!window.confirm(tip)) return;
    setBusy(true);
    try {
      var data = await api('/api/card/cloud/delete', {
        method: 'POST',
        body: JSON.stringify({
          cloudId: it.cloudId,
          isDraft: !!it.isDraft,
          characterId: linkedId,
          alsoHidePublished: alsoHide,
          cascadeDrafts: true,
        }),
      });
      if (!data.ok) {
        showMsg(data.error || '云端删除失败', false);
        return;
      }
      showMsg(
        (it.isDraft ? '已删草稿' : '已隐藏（删除）') + ' · ' + it.cloudId,
        true
      );
      await refreshCloudCardList(false);
      await refreshCardList();
    } catch (e) {
      showMsg(String(e.message || e), false);
    } finally {
      setBusy(false);
    }
  }

  function localListedMap() {
    var map = {};
    (window.__localCardListed || []).forEach(function (it) {
      var id = parseInt(it.cloudId, 10);
      if (id > 0) map[id] = !!it.isListed;
    });
    return map;
  }

  function makeBadge(cls, text, title) {
    var el = document.createElement('span');
    el.className = cls;
    el.textContent = text;
    if (title) el.title = title;
    return el;
  }

  /** 三行：1 名字 2 状态标签 3 编号/时间 */
  function buildCardItemMeta(name, statusNodes, subText) {
    var meta = document.createElement('div');
    meta.className = 'card-item-meta';
    var n = document.createElement('p');
    n.className = 'card-item-name';
    n.textContent = name || '—';
    var status = document.createElement('div');
    status.className = 'card-item-status card-item-badges';
    (statusNodes || []).forEach(function (node) {
      if (node) status.appendChild(node);
    });
    if (!status.childNodes.length) {
      status.appendChild(makeBadge('badge-draft', '本地', '尚未同步云端'));
    }
    var s = document.createElement('p');
    s.className = 'card-item-sub';
    s.textContent = subText || '';
    meta.appendChild(n);
    meta.appendChild(status);
    meta.appendChild(s);
    return meta;
  }

  function filteredCloudItems() {
    var q = String(cloudCardSearch || '').trim().toLowerCase();
    return (cloudCardItems || []).filter(function (it) {
      if (cloudCardFilter === 'draft' && !it.isDraft) return false;
      if (cloudCardFilter === 'pub' && it.isDraft) return false;
      if (!q) return true;
      var name = String(it.name || '').toLowerCase();
      var id = String(it.cloudId || '');
      var db = String(it.dbId || it.characterId || '');
      return name.indexOf(q) >= 0 || id.indexOf(q) >= 0 || (db && db.indexOf(q) >= 0);
    });
  }

  function bindCloudListScroll() {
    var sc = $('cloudCardListScroll');
    if (!sc || sc.getAttribute('data-bound') === '1') return;
    sc.setAttribute('data-bound', '1');
    sc.addEventListener('scroll', function () {
      if (sc.scrollTop + sc.clientHeight < sc.scrollHeight - 48) return;
      var filtered = filteredCloudItems();
      if (cloudVisibleCount >= filtered.length) return;
      var keep = sc.scrollTop;
      cloudVisibleCount = Math.min(filtered.length, cloudVisibleCount + CLOUD_PAGE_SIZE);
      renderCloudCardList();
      sc.scrollTop = keep;
    });
  }

  function renderCloudCardList(opts) {
    opts = opts || {};
    if (opts.resetPage) cloudVisibleCount = CLOUD_PAGE_SIZE;
    var box = $('cloudCardBox');
    var list = $('cloudCardList');
    var more = $('cloudCardMore');
    if (!box || !list) return;
    bindCloudListScroll();
    list.innerHTML = '';
    var listedMap = localListedMap();
    var filtered = filteredCloudItems();
    if (cloudVisibleCount < CLOUD_PAGE_SIZE) cloudVisibleCount = CLOUD_PAGE_SIZE;
    if (cloudVisibleCount > filtered.length && filtered.length > 0) {
      cloudVisibleCount = filtered.length;
    }
    var items = filtered.slice(0, cloudVisibleCount);
    box.hidden = false;
    if (more) {
      if (!filtered.length) {
        more.textContent = cloudCardSearch.trim() ? '没有匹配的卡' : '云端列表为空';
      } else if (items.length < filtered.length) {
        more.textContent = '显示 ' + items.length + ' / ' + filtered.length + ' · 下拉加载更多';
      } else {
        more.textContent = '共 ' + filtered.length + ' 张';
      }
    }
    items.forEach(function (it) {
      var cid = parseInt(it.cloudId, 10);
      // 优先用云端真实 isPublic / publishStatus；本地 _meta 仅作兜底
      var isListed = !it.isDraft && !!(it.isListed || it.isPublic || listedMap[cid]);
      var isPending = !it.isDraft && !isListed && !!(
        it.isPendingReview ||
        it.publishStatus === 'pending' ||
        it.publishStatus === 'pending_notified'
      );
      it.isListed = isListed;
      it.isPendingReview = isPending;
      var li = document.createElement('li');
      if (it.isDraft) li.className = 'card-tone-draft';
      else if (isListed) li.className = 'card-tone-listed';
      else if (isPending) li.className = 'card-tone-pending';
      else li.className = 'card-tone-saved';
      var statusNodes = [];
      if (it.isDraft) {
        statusNodes.push(makeBadge('badge-draft', '草稿', '云端草稿，可删除'));
      } else if (isListed) {
        statusNodes.push(makeBadge('badge-listed', '上架到广场', '已上架 · 下架请去官网'));
      } else if (isPending) {
        statusNodes.push(makeBadge('badge-pending', '审核中', '已提交上架，等待审核'));
      } else {
        statusNodes.push(makeBadge('badge-saved', '已保存', '正式卡 · 上架请去官网'));
      }
      if (it.isGamefy) {
        statusNodes.push(makeBadge('badge-gamefy', '游戏卡', '不可拉到本地写卡'));
      }
      var idBits = '#' + it.cloudId;
      if (it.isDraft && it.dbId) idBits += ' → 正式卡 #' + it.dbId;
      if (it.createdAt) idBits += ' · ' + it.createdAt;
      var meta = buildCardItemMeta(it.name || String(it.cloudId), statusNodes, idBits);
      var actions = document.createElement('div');
      actions.className = 'card-item-actions';

      if (!it.isGamefy) {
        var pullBtn = document.createElement('button');
        pullBtn.type = 'button';
        pullBtn.className = 'btn';
        pullBtn.textContent = '拉到本地';
        pullBtn.addEventListener('click', async function () {
          setBusy(true);
          try {
            var pulled = await api('/api/card/pull', {
              method: 'POST',
              body: JSON.stringify({
                cloudId: it.cloudId,
                name: it.name || '',
                isDraft: !!it.isDraft,
              }),
            });
            if (!pulled.ok) {
              showMsg(pulled.error || '拉取失败', false);
              return;
            }
            fillCardForm(pulled.card, pulled.localId, { mtime: pulled.mtime || 0 });
            currentCardFolder = pulled.folder || pulled.path || '';
            setStageMode('card');
            setCardFullscreen(true);
            await refreshCardList();
            showMsg('已拉到 卡/' + pulled.localId + '/', true);
          } catch (err) {
            showMsg(String(err.message || err), false);
          } finally {
            setBusy(false);
          }
        });
        actions.appendChild(pullBtn);
      }

      // 上架状态未知时不展示「隐藏」，避免误下架
      var listingKnown = it.isDraft || it.listingStatusKnown === true || it.isPublic != null || !!it.publishStatus || !!listedMap[cid];
      if (it.isDraft || (!it.isDraft && listingKnown && !isListed && !isPending && !it.isGamefy)) {
        var delBtn = document.createElement('button');
        delBtn.type = 'button';
        delBtn.className = 'btn danger';
        delBtn.textContent = it.isDraft ? '删除' : '隐藏';
        delBtn.title = it.isDraft ? '删除云端草稿' : '隐藏已保存卡（等同删除）';
        delBtn.addEventListener('click', function () { deleteCloudCard(it); });
        actions.appendChild(delBtn);
      }

      li.appendChild(meta);
      li.appendChild(actions);
      list.appendChild(li);
    });
    if (!items.length) {
      list.innerHTML = '<li><div class="card-item-meta"><p class="card-item-name">没有匹配项</p><div class="card-item-status"></div><p class="card-item-sub"></p></div></li>';
    }
  }

  async function refreshCloudCardList(showToast, opts) {
    opts = opts || {};
    var qs = opts.quick ? '?quick=1' : '';
    var data = await api('/api/card/cloud' + qs);
    if (!data.ok) {
      if (showToast !== false && !opts.quiet) {
        showMsg(data.error || '云端列表失败（需已登录）', false);
      }
      // 轮询失败不强制藏列表；手动拉取仍隐藏
      if (!opts.quiet && $('cloudCardBox')) $('cloudCardBox').hidden = true;
      return data;
    }
    if (!opts.skipLocal) {
      try { await refreshCardList({ quiet: !!opts.quiet }); } catch (_) {}
    }
    var items = data.items || [];
    var sig = listItemsSig(items, 'cloud');
    // quick 轮询若与上次完整列表条数/id 一致则合并保留上架字段，避免绿标闪烁
    if (opts.quick && cloudCardItems.length && items.length === cloudCardItems.length) {
      var prevMap = {};
      cloudCardItems.forEach(function (it) {
        prevMap[(it.isDraft ? 'd' : 'p') + ':' + it.cloudId] = it;
      });
      items.forEach(function (it) {
        var prev = prevMap[(it.isDraft ? 'd' : 'p') + ':' + it.cloudId];
        if (!prev) return;
        if (it.isListed == null && prev.isListed != null) it.isListed = prev.isListed;
        if (it.isPendingReview == null && prev.isPendingReview != null) it.isPendingReview = prev.isPendingReview;
        if (!it.publishStatus && prev.publishStatus) it.publishStatus = prev.publishStatus;
        if (!it.isPublic && prev.isPublic) it.isPublic = prev.isPublic;
      });
      sig = listItemsSig(items, 'cloud');
    }
    if (opts.quiet && sig === lastCloudListSig) return data;
    lastCloudListSig = sig;
    cloudCardItems = items;
    renderCloudCardList();
    var drafts = cloudCardItems.filter(function (x) { return x.isDraft; }).length;
    var pubs = cloudCardItems.length - drafts;
    if (showToast !== false && !opts.quiet) {
      showMsg('云端 ' + cloudCardItems.length + ' 项 · 草稿 ' + drafts + ' · 已保存 ' + pubs, true);
    }
    return data;
  }

  async function refreshCardList(opts) {
    opts = opts || {};
    try {
      var data = await api('/api/card/list');
      var list = $('cardList');
      if (!list) return data;
      if (!data.ok) {
        if (!opts.quiet) setCardMsg(data.error || '列表失败', false);
        return data;
      }
      var items = data.items || [];
      var sig = listItemsSig(items, 'local');
      if (opts.quiet && sig === lastLocalListSig) return data;
      lastLocalListSig = sig;
      list.innerHTML = '';
      window.__localCardListed = items;
      if (!opts.quiet) {
        setCardMsg('本机 ' + items.length + ' 张 · ' + (data.cardsDir || '卡/'), true);
      }
      items.forEach(function (it) {
        var li = document.createElement('li');
        var statusNodes = [];
        var isPending = !!(it.isPendingReview || it.publishStatus === 'pending' || it.publishStatus === 'pending_notified');
        var isSaved = !!(it.published || (it.cloudId && Number(it.cloudId) > 0));
        if (it.isListed) {
          li.className = 'card-tone-listed';
          statusNodes.push(makeBadge('badge-listed', '上架到广场', '已上架 · #' + it.cloudId));
        } else if (isPending) {
          li.className = 'card-tone-pending';
          statusNodes.push(makeBadge('badge-pending', '审核中', '已提交上架 · #' + it.cloudId));
        } else if (isSaved) {
          li.className = 'card-tone-saved';
          statusNodes.push(makeBadge('badge-saved', '已保存', '正式卡 · #' + it.cloudId + ' · 上架请去官网'));
        } else if (it.draftId) {
          li.className = 'card-tone-draft';
          statusNodes.push(makeBadge('badge-draft', '草稿', 'draftId · ' + it.draftId));
        } else {
          li.className = 'card-tone-local';
          statusNodes.push(makeBadge('badge-draft', '本地', '尚未同步云端'));
        }
        var sub = '本地 · ' + it.localId;
        if (it.cloudId) sub += ' · #' + it.cloudId;
        else if (it.draftId) sub += ' · draft #' + it.draftId;
        if (it.updatedAt) sub += ' · ' + it.updatedAt;
        var meta = buildCardItemMeta(it.name || it.localId, statusNodes, sub);
        var actions = document.createElement('div');
        actions.className = 'card-item-actions';
        var btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'btn';
        btn.textContent = '打开';
        btn.addEventListener('click', function () { openLocalCard(it.localId); });
        var del = document.createElement('button');
        del.type = 'button';
        del.className = 'btn danger';
        del.textContent = '删除';
        del.addEventListener('click', function () { deleteLocalCard(it.localId); });
        actions.appendChild(btn);
        actions.appendChild(del);
        li.appendChild(meta);
        li.appendChild(actions);
        list.appendChild(li);
      });
      return data;
    } catch (e) {
      if (!opts.quiet) setCardMsg(String(e.message || e), false);
      return { ok: false, error: String(e.message || e) };
    }
  }

  async function openLocalCard(localId) {
    setBusy(true);
    try {
      var data = await api('/api/card/get?id=' + encodeURIComponent(localId));
      if (!data.ok) {
        showMsg(data.error || '打开失败', false);
        return;
      }
      fillCardForm(data.card, data.localId || localId, {
        mtime: data.mtime || 0,
        sig: data.sig || '',
      });
      currentCardFolder = data.folder || '';
      setStageMode('card');
      setCardFullscreen(true);
      showMsg('已打开卡夹 · ' + (data.folder || data.localId || localId), true);
    } catch (e) {
      showMsg(String(e.message || e), false);
    } finally {
      setBusy(false);
    }
  }

  async function saveCurrentCard() {
    setBusy(true);
    showMsg('正在写入卡夹…', true);
    try {
      var card = collectCardFromForm();
      if (cardLooksEmpty(card) && currentCard && !cardLooksEmpty(currentCard)) {
        showMsg('表单几乎为空，已取消保存，避免覆盖本地已有内容。请先「重载当前」。', false);
        return;
      }
      var folderName = ($('cardFolderName') && $('cardFolderName').value.trim()) || currentCardId || card.data.name;
      var forceOverwrite = false;
      if (currentCardId && folderName && folderName !== currentCardId) {
        var exists = (window.__localCardListed || []).some(function (it) {
          return it && it.localId === folderName;
        });
        if (exists) {
          if (!window.confirm('卡夹「' + folderName + '」已存在，确定覆盖吗？')) {
            showMsg('已取消保存', false);
            return;
          }
          forceOverwrite = true;
        }
      }
      var data = await api('/api/card/save', {
        method: 'POST',
        body: JSON.stringify({
          localId: folderName,
          previousLocalId: currentCardId || '',
          force: forceOverwrite,
          brief: getCardBrief(),
          card: card,
        }),
      });
      if (!data.ok) {
        showMsg(data.error || '保存失败', false);
        return;
      }
      fillCardForm(data.card, data.localId, { mtime: data.mtime || 0 });
      currentCardFolder = data.folder || data.path || '';
      await refreshCardList();
      showMsg('已写入 · ' + (data.folder || ('卡/' + data.localId)), true);
    } catch (e) {
      showMsg(String(e.message || e), false);
    } finally {
      setBusy(false);
    }
  }

  var lastPullJob = null;

  function confirmRetryFailedPull(job) {
    var failed = (job && job.failedPaths) || [];
    if (!failed.length) {
      showMsg('当前没有失败文件可重拉', false);
      return false;
    }
    var lines = [
      '将只重拉上次失败的 ' + failed.length + ' 个文件（不会全量拉取）。',
      '',
    ];
    failed.slice(0, 10).forEach(function (p) { lines.push('· ' + p); });
    if (failed.length > 10) lines.push('· … 另有 ' + (failed.length - 10) + ' 个');
    if (job && job.out) {
      lines.push('');
      lines.push('输出目录：' + job.out);
    }
    lines.push('', '继续？');
    return window.confirm(lines.join('\n'));
  }

  function renderPull(job) {
    if (!job) return;
    lastPullJob = job;
    var total = job.total || 0;
    var current = job.current || 0;
    var pct = total > 0 ? Math.min(100, Math.round((current / total) * 100)) : (job.done ? 100 : 0);
    if (job.running && total === 0) pct = 8;
    $('pullBar').style.width = pct + '%';

    var failedPaths = job.failedPaths || [];
    var canRetry = !!(!job.running && (job.canRetryFailed || failedPaths.length > 0));
    var retryBtn = $('pullRetryBtn');
    var retryHint = $('pullRetryHint');
    if (retryBtn) retryBtn.hidden = !canRetry;
    if (retryHint) {
      retryHint.hidden = !canRetry;
      if (canRetry) {
        retryHint.textContent =
          '提醒：还有 ' + failedPaths.length + ' 个失败文件，请点「重拉失败文件」只补下这些（不会全量重拉）。';
        retryHint.classList.add('is-alert');
      } else {
        retryHint.textContent = '有失败文件时可点「重拉失败文件」只补下失败项，不会全量重拉。';
        retryHint.classList.remove('is-alert');
      }
    }

    var lines = [];
    if (job.message) lines.push(job.message);
    if (job.mode === 'retry') lines.push('模式: 只重拉失败文件');
    if (total) lines.push('进度 ' + current + '/' + total + ' · 成功 ' + (job.okCount || 0) + ' · 失败 ' + (job.failCount || 0));
    if (job.out) lines.push('输出目录: ' + job.out);
    if (job.error) lines.push('错误: ' + job.error);
    if (failedPaths.length && !job.running) {
      lines.push('失败文件 ' + failedPaths.length + ' 个（请点上方「重拉失败文件」）:');
      failedPaths.slice(0, 12).forEach(function (p) { lines.push('  - ' + p); });
      if (failedPaths.length > 12) lines.push('  … 另有 ' + (failedPaths.length - 12) + ' 个');
    }
    if (job.logs && job.logs.length) {
      lines.push('---');
      lines = lines.concat(job.logs.slice(-18));
    }
    $('pullLog').textContent = lines.join('\n') || '尚未开始';
  }

  function fillOriginSelect(status) {
    var sel = $('originSelect');
    if (!sel) return;
    var origins = status.origins || [];
    var cur = status.origin || '';
    var html = origins.map(function (o) {
      return '<option value="' + o.replace(/"/g, '&quot;') + '"' + (o === cur ? ' selected' : '') + '>' + o.replace(/^https:\/\//, '') + '</option>';
    }).join('');
    if (cur && origins.indexOf(cur) < 0) {
      html = '<option value="' + cur.replace(/"/g, '&quot;') + '" selected>' + cur.replace(/^https:\/\//, '') + '</option>' + html;
    }
    sel.innerHTML = html || '<option value="https://www.dzmm.ai">www.dzmm.ai</option>';
    if (cur) sel.value = cur;
    var link = $('addrPageLink');
    if (link && status.addrPage) link.href = status.addrPage;
    var hint = $('originHint');
    if (hint) {
      var hostOnly = String(cur || '').replace(/^https?:\/\//, '').replace(/^www\./, '');
      if (cur && /dzmm\.ai$/i.test(hostOnly)) {
        hint.hidden = false;
        hint.classList.add('is-alert');
        hint.textContent = '当前是海外主站 www.dzmm.ai。若登录/拉取被拦截，请点「测速并优选」或手动换国内线路后重新登录。';
      } else {
        hint.hidden = false;
        hint.classList.remove('is-alert');
        hint.textContent = '当前线路：' + (cur || '未设置') + '（换线路后需重新登录）';
      }
    }
  }

  function escapeHtml(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  function normPathKey(p) {
    return String(p || '').replace(/\//g, '\\').toLowerCase();
  }

  function renderProjectHistory(status) {
    var list = $('projectHistoryList');
    var empty = $('projectHistoryEmpty');
    if (!list) return;
    var hist = (status && status.projectHistory) || [];
    var curCid = String((status && status.characterId) || ($('characterId') && $('characterId').value) || '');
    var curPath = normPathKey((status && status.projectPath) || ($('projectPath') && $('projectPath').value) || '');
    if (!hist.length) {
      list.innerHTML = '';
      if (empty) empty.hidden = false;
      return;
    }
    if (empty) empty.hidden = true;
    list.innerHTML = hist.map(function (it) {
      var cid = String(it.character_id || '');
      var path = String(it.project_path || '');
      var label = String(it.label || path || cid);
      var active = cid === curCid && normPathKey(path) === curPath;
      return (
        '<li class="project-history-item' + (active ? ' is-active' : '') + '" data-cid="' + escapeHtml(cid) + '" data-path="' + escapeHtml(path) + '">' +
          '<button type="button" class="project-history-main" data-act="switch" title="切换到此项目">' +
            '<span class="project-history-name">' + escapeHtml(label) + (active ? ' · 当前' : '') + '</span>' +
            '<span class="project-history-meta">#' + escapeHtml(cid) + ' · ' + escapeHtml(path) + '</span>' +
          '</button>' +
          '<button type="button" class="project-history-remove" data-act="remove" title="从历史移除">×</button>' +
        '</li>'
      );
    }).join('');
  }

  async function switchProjectFromHistory(cid, path) {
    if (!cid || !path) return;
    try {
      var syncState = await api('/api/sync');
      if (syncState && syncState.job && syncState.job.running) {
        showMsg('同步进行中，请先等同步结束再切换项目', false);
        return;
      }
    } catch (_) {}
    setBusy(true);
    try {
      var data = await api('/api/config', {
        method: 'POST',
        body: JSON.stringify({
          email: $('email') ? $('email').value.trim() : undefined,
          characterId: cid,
          projectPath: path,
          previewPort: $('previewPort') ? $('previewPort').value : undefined,
        }),
      });
      if (data.status) fillForm(data.status);
      showMsg(data.ok ? ('已切换到 #' + cid + ' · ' + path) : (data.error || '切换失败'), !!data.ok);
    } catch (e) {
      showMsg(String(e.message || e), false);
    } finally {
      setBusy(false);
    }
  }

  async function removeProjectFromHistory(cid, path) {
    if (!cid || !path) return;
    setBusy(true);
    try {
      var data = await api('/api/config/history/remove', {
        method: 'POST',
        body: JSON.stringify({ characterId: cid, projectPath: path }),
      });
      if (data.status) fillForm(data.status);
      showMsg(data.ok ? '已从历史移除' : (data.error || '移除失败'), !!data.ok);
    } catch (e) {
      showMsg(String(e.message || e), false);
    } finally {
      setBusy(false);
    }
  }

  function fillForm(status, opts) {
    opts = opts || {};
    if (!status) return;
    if (status.email && !$('email').value) $('email').value = status.email;
    if (status.characterId) $('characterId').value = status.characterId;
    if (status.projectPath) $('projectPath').value = status.projectPath;
    if (status.previewPort) $('previewPort').value = status.previewPort;
    fillOriginSelect(status);
    renderProjectHistory(status);
    var pathHint = $('projectPathHint');
    if (pathHint) {
      var resolveErr = status.projectResolveError || '';
      var resolveHint = status.projectResolveHint || '';
      var pullFailed = Number(status.pullFailedCount || 0) || 0;
      if (resolveErr) {
        pathHint.hidden = false;
        pathHint.classList.add('is-alert');
        pathHint.textContent = resolveErr.replace(/\n/g, ' ');
      } else if (resolveHint) {
        pathHint.hidden = false;
        pathHint.classList.remove('is-alert');
        pathHint.textContent = resolveHint;
      } else if (pullFailed > 0 && !status.publishIndexExists) {
        pathHint.hidden = false;
        pathHint.classList.add('is-alert');
        pathHint.textContent = '路径已设置，但还有 ' + pullFailed + ' 个拉取失败文件；请到「拉取容器」重拉失败文件后再预览。';
      } else {
        pathHint.hidden = true;
        pathHint.classList.remove('is-alert');
        pathHint.textContent = '';
      }
    }
    var link = $('workbenchLink');
    if (status.workbenchUrl) {
      link.href = status.workbenchUrl;
      link.removeAttribute('aria-disabled');
    } else {
      link.href = '#';
    }
    var lamp = $('lamp');
    if (status.loggedIn) {
      lamp.className = 'lamp on';
      lamp.textContent = '已登录';
    } else {
      lamp.className = 'lamp off';
      lamp.textContent = '未登录';
    }
    if (consoleMode === 'card') {
      // 角色卡只要账号态：登录 / 线路 / 剩余时长；不展示游戏 characterId / 工程路径
      $('statusBox').textContent = JSON.stringify({
        mode: 'card',
        loggedIn: status.loggedIn,
        email: status.emailMasked || status.email,
        remainSec: status.remainSec,
        origin: status.origin || '',
        hasPassword: status.hasPassword,
        error: status.error || '',
      }, null, 2);
    } else {
      $('statusBox').textContent = JSON.stringify({
        loggedIn: status.loggedIn,
        email: status.emailMasked || status.email,
        remainSec: status.remainSec,
        characterId: status.characterId,
        projectPath: status.projectPath || '(未设置)',
        previewPort: status.previewPort,
        origin: status.origin || '',
        workbenchUrl: status.workbenchUrl || '',
        kitRoot: status.kitRoot,
        hasPassword: status.hasPassword,
        error: status.error || '',
      }, null, 2);
    }
    if (status.pull && consoleMode === 'game') renderPull(status.pull);
    if (!opts.skipPreview && status.preview && consoleMode === 'game') renderPreview(status.preview);
    if (consoleMode === 'card') detachGameRuntime();
  }

  async function api(path, options) {
    var res = await fetch(path, Object.assign({
      headers: { 'Content-Type': 'application/json' },
    }, options || {}));
    var data = await res.json().catch(function () { return { ok: false, error: '无效响应' }; });
    if (!res.ok && !data.error) data.error = 'HTTP ' + res.status;
    return data;
  }

  async function refresh() {
    var status = await api('/api/status');
    fillForm(status);
    return status;
  }

  function stopPullPoll() {
    if (pullTimer) {
      clearInterval(pullTimer);
      pullTimer = null;
    }
  }

  function startPullPoll() {
    stopPullPoll();
    pullTimer = setInterval(async function () {
      try {
        var data = await api('/api/pull');
        renderPull(data.job);
        if (data.job && !data.job.running && data.job.done) {
          stopPullPoll();
          setBusy(false);
          await refresh();
          var failedN = ((data.job.failedPaths) || []).length || Number(data.job.failCount || 0) || 0;
          var stillFail = !!(data.job.error && !(data.job.result && data.job.result.ok)) || failedN > 0;
          if (stillFail && failedN > 0) {
            showMsg(
              (data.job.mode === 'retry' ? '重拉结束' : '拉取结束') +
                '：仍有 ' + failedN + ' 个失败 · 请点「重拉失败文件」继续补下',
              false
            );
            switchPanel('pull');
          } else if (stillFail) {
            showMsg(data.job.error || data.job.message || '拉取结束（有失败）', false);
          } else if (data.job.mode === 'retry') {
            showMsg(data.job.message || '失败文件已全部重拉成功', true);
          } else {
            showMsg(data.job.message || '拉取完成', true);
          }
        }
      } catch (_) {}
    }, 800);
  }

  if ($('originSelect')) {
    $('originSelect').addEventListener('change', async function () {
      var origin = $('originSelect').value;
      if (!origin) return;
      if (!confirm('切换到 ' + origin + '？\n换域后会清除本机 cookie，需要重新登录。')) {
        await refresh();
        return;
      }
      setBusy(true);
      showMsg('正在切换线路…', true);
      try {
        var data = await api('/api/origin', {
          method: 'POST',
          body: JSON.stringify({ origin: origin }),
        });
        if (data.status) fillForm(data.status);
        showMsg(data.message || (data.ok ? '线路已切换，请重新登录' : (data.error || '切换失败')), !!data.ok);
      } catch (e) {
        showMsg(String(e.message || e), false);
      } finally {
        setBusy(false);
      }
    });
  }

  if ($('originProbeBtn')) {
    $('originProbeBtn').addEventListener('click', async function () {
      setBusy(true);
      showMsg('正在测速各线路（约数秒）…', true);
      try {
        var data = await api('/api/origin', {
          method: 'POST',
          body: JSON.stringify({ auto: true }),
        });
        if (data.status) fillForm(data.status);
        else if (data.origin) fillOriginSelect({ origin: data.origin, origins: (data.results || []).map(function (r) { return r.origin; }), addrPage: data.addrPage });
        showMsg(data.message || (data.ok ? ('已优选 ' + data.origin) : (data.error || '测速失败')), !!data.ok);
      } catch (e) {
        showMsg(String(e.message || e), false);
      } finally {
        setBusy(false);
      }
    });
  }

  function loginSidePayload() {
    return {
      characterId: $('characterId') ? $('characterId').value : undefined,
      projectPath: $('projectPath') ? $('projectPath').value.trim() : undefined,
      previewPort: $('previewPort') ? $('previewPort').value : undefined,
      origin: $('originSelect') ? $('originSelect').value : undefined,
    };
  }

  function switchLoginTab(tab) {
    var tabs = document.querySelectorAll('#loginTabs .login-tab');
    var panes = document.querySelectorAll('#loginForm .login-pane');
    tabs.forEach(function (btn) {
      btn.classList.toggle('active', btn.getAttribute('data-login-tab') === tab);
    });
    panes.forEach(function (pane) {
      var on = pane.getAttribute('data-login-pane') === tab;
      pane.classList.toggle('active', on);
      pane.hidden = !on;
    });
    if (tab !== 'telegram') stopTelegramPoll();
  }

  var tgPollTimer = null;
  var tgSignInCode = '';

  function stopTelegramPoll() {
    if (tgPollTimer) {
      clearInterval(tgPollTimer);
      tgPollTimer = null;
    }
    tgSignInCode = '';
    if ($('tgStopBtn')) $('tgStopBtn').disabled = true;
  }

  async function runLogout() {
    setBusy(true);
    try {
      stopTelegramPoll();
      var data = await api('/api/logout', { method: 'POST', body: '{}' });
      if (data.status) fillForm(data.status);
      showMsg('已清除本地登录 cookie', true);
    } catch (e) {
      showMsg(String(e.message || e), false);
    } finally {
      setBusy(false);
    }
  }

  if ($('loginTabs')) {
    $('loginTabs').addEventListener('click', function (ev) {
      var btn = ev.target && ev.target.closest ? ev.target.closest('[data-login-tab]') : null;
      if (!btn) return;
      switchLoginTab(btn.getAttribute('data-login-tab'));
    });
  }

  $('loginForm').addEventListener('submit', async function (ev) {
    ev.preventDefault();
    var active = document.querySelector('#loginForm .login-pane.active');
    var pane = active ? active.getAttribute('data-login-pane') : 'password';
    if (pane !== 'password') return;
    setBusy(true);
    showMsg('正在登录…', true);
    try {
      var payload = loginSidePayload();
      payload.email = $('email').value.trim();
      payload.password = $('password').value;
      payload.savePassword = $('savePassword').checked;
      var data = await api('/api/login', {
        method: 'POST',
        body: JSON.stringify(payload),
      });
      if (data.status) fillForm(data.status);
      if (data.ok) {
        showMsg('登录成功 · 剩余约 ' + data.remainSec + 's', true);
        $('password').value = '';
        if (data.bridge) renderBridge(data.bridge, data.preview || {});
        else refreshBridge(true);
      } else {
        showMsg(data.error || '登录失败', false);
      }
    } catch (e) {
      showMsg(String(e.message || e), false);
    } finally {
      setBusy(false);
    }
  });

  if ($('loginCodeBtn')) {
    $('loginCodeBtn').addEventListener('click', async function () {
      var input = $('loginCodeFile');
      var file = input && input.files && input.files[0];
      if (!file) {
        showMsg('请先选择登录码图片', false);
        return;
      }
      setBusy(true);
      showMsg('正在识别登录码…', true);
      try {
        var dataUrl = await new Promise(function (resolve, reject) {
          var reader = new FileReader();
          reader.onload = function () { resolve(String(reader.result || '')); };
          reader.onerror = function () { reject(new Error('读取图片失败')); };
          reader.readAsDataURL(file);
        });
        var payload = loginSidePayload();
        payload.imageBase64 = dataUrl;
        payload.filename = file.name || 'sign-in-code.jpg';
        var data = await api('/api/login/code', {
          method: 'POST',
          body: JSON.stringify(payload),
        });
        if (data.status) fillForm(data.status);
        if (data.ok) {
          showMsg('登录码登录成功 · 剩余约 ' + data.remainSec + 's', true);
          if (input) input.value = '';
          if (data.bridge) renderBridge(data.bridge, data.preview || {});
          else refreshBridge(true);
        } else {
          showMsg(data.error || '登录码登录失败', false);
        }
      } catch (e) {
        showMsg(String(e.message || e), false);
      } finally {
        setBusy(false);
      }
    });
  }

  if ($('cookieLoginBtn')) {
    $('cookieLoginBtn').addEventListener('click', async function () {
      var cookie = ($('cookieInput') && $('cookieInput').value.trim()) || '';
      if (!cookie) {
        showMsg('请粘贴 Cookie', false);
        return;
      }
      setBusy(true);
      showMsg('正在校验 Cookie…', true);
      try {
        var payload = loginSidePayload();
        payload.cookie = cookie;
        var data = await api('/api/login/cookie', {
          method: 'POST',
          body: JSON.stringify(payload),
        });
        if (data.status) fillForm(data.status);
        if (data.ok) {
          showMsg('Cookie 登录成功 · 剩余约 ' + data.remainSec + 's', true);
          if ($('cookieInput')) $('cookieInput').value = '';
          if (data.bridge) renderBridge(data.bridge, data.preview || {});
          else refreshBridge(true);
        } else {
          showMsg(data.error || 'Cookie 登录失败', false);
        }
      } catch (e) {
        showMsg(String(e.message || e), false);
      } finally {
        setBusy(false);
      }
    });
  }

  if ($('tgStartBtn')) {
    $('tgStartBtn').addEventListener('click', async function () {
      setBusy(true);
      showMsg('正在获取 Telegram 登录码…', true);
      stopTelegramPoll();
      try {
        var data = await api('/api/login/telegram/start', {
          method: 'POST',
          body: JSON.stringify(loginSidePayload()),
        });
        if (!data.ok) {
          showMsg(data.error || '获取失败', false);
          return;
        }
        tgSignInCode = data.signInCode || '';
        if ($('tgBox')) $('tgBox').hidden = false;
        if ($('tgQrImg')) {
          var svg = String(data.qrCodeSvg || '');
          if (svg.indexOf('data:') === 0) {
            $('tgQrImg').src = svg;
          } else if (svg.indexOf('<svg') >= 0) {
            $('tgQrImg').src = 'data:image/svg+xml;charset=utf-8,' + encodeURIComponent(svg);
          } else if (data.qrCodeUrl) {
            // 无可用 SVG 时用 t.me 链接作提示（二维码图可能缺失）
            $('tgQrImg').removeAttribute('src');
            $('tgQrImg').alt = '请点上方链接打开 Telegram，或向 Bot 发送 /login ' + (data.signInCode || '');
          } else {
            $('tgQrImg').src = '';
          }
        }
        if ($('tgQrLink')) $('tgQrLink').href = data.qrCodeUrl || '#';
        if ($('tgMeta')) {
          $('tgMeta').textContent =
            'Bot @' + (data.botUsername || '') +
            ' · 指令 /login ' + (data.signInCode || '') +
            (data.createdAt ? ' · 创建于 ' + data.createdAt : '');
        }
        if ($('tgStatus')) $('tgStatus').textContent = '等待 Telegram 确认…';
        if ($('tgStopBtn')) $('tgStopBtn').disabled = false;
        showMsg('请用 Telegram 扫码或发送 /login ' + tgSignInCode, true);
        tgPollTimer = setInterval(async function () {
          if (!tgSignInCode) return;
          try {
            var poll = await api('/api/login/telegram/poll', {
              method: 'POST',
              body: JSON.stringify({ signInCode: tgSignInCode }),
            });
            if (poll.status) fillForm(poll.status);
            if (poll.ok && poll.loggedIn) {
              stopTelegramPoll();
              showMsg('Telegram 登录成功 · 剩余约 ' + poll.remainSec + 's', true);
              if ($('tgStatus')) $('tgStatus').textContent = '已登录';
              if (poll.bridge) renderBridge(poll.bridge, poll.preview || {});
              else refreshBridge(true);
              return;
            }
            if ($('tgStatus')) {
              $('tgStatus').textContent =
                (poll.tgStatus || 'waiting') + (poll.message ? ' · ' + poll.message : '');
            }
            if (poll.tgStatus === 'code_expired' || poll.tgStatus === 'user_rejected' || poll.tgStatus === 'login_failed') {
              stopTelegramPoll();
              showMsg(poll.message || ('Telegram 登录结束：' + poll.tgStatus), false);
            }
          } catch (e) {
            if ($('tgStatus')) $('tgStatus').textContent = String(e.message || e);
          }
        }, 1500);
      } catch (e) {
        showMsg(String(e.message || e), false);
      } finally {
        setBusy(false);
      }
    });
  }

  if ($('tgStopBtn')) {
    $('tgStopBtn').addEventListener('click', function () {
      stopTelegramPoll();
      if ($('tgStatus')) $('tgStatus').textContent = '已停止等待';
      showMsg('已停止等待 Telegram', true);
    });
  }

  $('configForm').addEventListener('submit', async function (ev) {
    ev.preventDefault();
    setBusy(true);
    try {
      var data = await api('/api/config', {
        method: 'POST',
        body: JSON.stringify({
          email: $('email').value.trim(),
          characterId: $('characterId').value,
          projectPath: $('projectPath').value.trim(),
          previewPort: $('previewPort').value,
        }),
      });
      if (data.status) fillForm(data.status);
      showMsg(data.ok ? '配置已保存' : (data.error || '保存失败'), !!data.ok);
    } catch (e) {
      showMsg(String(e.message || e), false);
    } finally {
      setBusy(false);
    }
  });

  if ($('projectHistoryList')) {
    $('projectHistoryList').addEventListener('click', function (ev) {
      var t = ev.target;
      if (!t) return;
      var btn = t.closest ? t.closest('[data-act]') : null;
      if (!btn) return;
      var row = btn.closest ? btn.closest('.project-history-item') : null;
      if (!row) return;
      var cid = row.getAttribute('data-cid') || '';
      var path = row.getAttribute('data-path') || '';
      var act = btn.getAttribute('data-act');
      if (act === 'switch') {
        switchProjectFromHistory(cid, path);
      } else if (act === 'remove') {
        removeProjectFromHistory(cid, path);
      }
    });
  }

  ['logoutBtn', 'logoutBtn2', 'logoutBtn3', 'logoutBtn4'].forEach(function (id) {
    if ($(id)) $(id).addEventListener('click', runLogout);
  });

  $('pingBtn').addEventListener('click', async function () {
    setBusy(true);
    showMsg('正在连接编辑器…', true);
    try {
      var data = await api('/api/ping', { method: 'POST', body: '{}' });
      if (data.status) fillForm(data.status);
      if (data.ok) {
        showMsg('编辑器已连接 · gameId=' + data.gameId + ' · ' + (data.editorStatus || ''), true);
      } else {
        if (data.captchaUrl) {
          try { window.open(data.captchaUrl, '_blank', 'noopener'); } catch (_) {}
          showMsg((data.error || '需要验证码') + ' · 已尝试打开验证页', false);
        } else {
          showMsg(data.error || '连接失败', false);
        }
      }
    } catch (e) {
      showMsg(String(e.message || e), false);
    } finally {
      setBusy(false);
    }
  });

  $('previewStartBtn').addEventListener('click', async function () {
    setBusy(true);
    showMsg(previewSource === 'cloud' ? '正在启动云端预览…' : '正在启动本地预览…', true);
    try {
      await api('/api/config', {
        method: 'POST',
        body: JSON.stringify({
          email: $('email').value.trim(),
          characterId: $('characterId').value,
          projectPath: $('projectPath').value.trim(),
          previewPort: $('previewPort').value,
        }),
      });
      var data;
      if (previewSource === 'cloud') {
        data = await api('/api/preview/source', {
          method: 'POST',
          body: JSON.stringify({
            source: 'cloud',
            characterId: $('characterId').value,
            projectPath: $('projectPath').value.trim(),
            previewPort: $('previewPort').value,
          }),
        });
      } else {
        data = await api('/api/preview/start', {
          method: 'POST',
          body: JSON.stringify({
            characterId: $('characterId').value,
            projectPath: $('projectPath').value.trim(),
            previewPort: $('previewPort').value,
            source: 'local',
          }),
        });
      }
      if (data.status) fillForm(data.status);
      else if (data.preview) renderPreview(data.preview);
      if (data.ok) {
        setPreviewSourceUi(data.source || previewSource);
        cloudPreviewRevision = Number((data.cloud && data.cloud.revision) || cloudPreviewRevision || 0);
        var url = data.url || (data.preview && data.preview.url) || '';
        showMsg((previewSource === 'cloud' ? '云端' : '本地') + '预览已启动 · ' + url, true);
        if (url) {
          $('previewLink').href = url;
          $('previewFrame').dataset.url = '';
          renderPreview(Object.assign({}, data.preview || {}, {
            running: true,
            url: url,
            source: previewSource,
            cloud: data.cloud || (data.preview && data.preview.cloud) || {},
          }));
          switchPanel('bridge');
          startBridgePoll();
          if (previewSource === 'cloud') startCloudPreviewPoll();
        }
      } else {
        var errText = data.error || '预览启动失败';
        showMsg(String(errText).split('\n')[0], false);
        if (data.canRetryFailed || /重拉失败文件/.test(String(errText))) {
          if (window.confirm(String(errText) + '\n\n现在去「拉取容器」重拉失败文件？')) {
            switchPanel('pull');
          }
        } else if (String(errText).indexOf('\n') >= 0) {
          window.alert(String(errText));
        }
      }
    } catch (e) {
      showMsg(String(e.message || e), false);
    } finally {
      setBusy(false);
    }
  });

  $('previewStopBtn').addEventListener('click', async function () {
    setBusy(true);
    try {
      var data = await api('/api/preview/stop', { method: 'POST', body: '{}' });
      if (data.status) fillForm(data.status);
      else if (data.preview) renderPreview(data.preview);
      $('previewFrame').src = 'about:blank';
      $('previewFrame').dataset.url = '';
      setStageLive(false);
      stopBridgePoll();
      stopCloudPreviewPoll();
      setPreviewSourceUi('local');
      renderBridge(null, { running: false });
      showMsg('预览已停止', true);
    } catch (e) {
      showMsg(String(e.message || e), false);
    } finally {
      setBusy(false);
    }
  });

  if ($('previewSrcLocal')) {
    $('previewSrcLocal').addEventListener('click', function () {
      if (previewSource === 'local') return;
      switchPreviewSource('local');
    });
  }
  if ($('previewSrcCloud')) {
    $('previewSrcCloud').addEventListener('click', function () {
      if (previewSource === 'cloud') return;
      switchPreviewSource('cloud');
    });
  }

  $('bridgeRefreshBtn').addEventListener('click', async function () {
    setBusy(true);
    try {
      var data = await refreshBridge(true);
      if (data && data.ok) {
        var name = (data.bridge && (data.bridge.displayName || data.bridge.email)) || '';
        showMsg(name ? ('桥接已刷新 · 链接用户 ' + name) : '桥接状态已刷新', true);
      } else {
        showMsg((data && data.error) || '预览未启动', false);
      }
    } finally {
      setBusy(false);
    }
  });

  $('publishBtn').addEventListener('click', function () {
    runPublish({ direct: false });
  });

  function bindCollapse(id) {
    var el = $(id);
    if (!el) return;
    el.addEventListener('click', function () {
      setSidebarCollapsed(true);
    });
  }
  bindCollapse('sidebarCollapseBtn');
  bindCollapse('sidebarCollapseBtn2');

  function applyFabPos(left, top) {
    var dock = $('fabDock');
    if (!dock) return;
    var pad = 8;
    var rect = dock.getBoundingClientRect();
    var w = rect.width || 168;
    var h = rect.height || 168;
    var maxL = Math.max(pad, window.innerWidth - w - pad);
    var maxT = Math.max(pad, window.innerHeight - h - pad);
    var l = Math.min(maxL, Math.max(pad, left));
    var t = Math.min(maxT, Math.max(pad, top));
    dock.style.left = l + 'px';
    dock.style.top = t + 'px';
    dock.style.right = 'auto';
    dock.style.bottom = 'auto';
    return { left: l, top: t };
  }

  function restoreFabPos() {
    try {
      var raw = localStorage.getItem(FAB_POS_KEY);
      if (!raw) return;
      var pos = JSON.parse(raw);
      if (pos && typeof pos.left === 'number' && typeof pos.top === 'number') {
        applyFabPos(pos.left, pos.top);
      }
    } catch (_) {}
  }

  function bindFabHover() {
    var dock = $('fabDock');
    if (!dock) return;
    var leaveTimer = null;
    function openFab() {
      if (leaveTimer) {
        clearTimeout(leaveTimer);
        leaveTimer = null;
      }
      dock.classList.add('is-open');
    }
    function scheduleClose() {
      if (leaveTimer) clearTimeout(leaveTimer);
      // 稍延迟再收起，方便鼠标滑到卫星键
      leaveTimer = setTimeout(function () {
        dock.classList.remove('is-open');
        leaveTimer = null;
      }, 160);
    }
    dock.addEventListener('pointerenter', openFab);
    dock.addEventListener('pointerleave', scheduleClose);
    // 触屏：点中心附近也可保持展开一会儿
    dock.addEventListener('pointerdown', function () {
      openFab();
    });
  }

  function bindFabDrag() {
    var dock = $('fabDock');
    var handle = $('fabPublish');
    if (!dock || !handle) return;

    var dragging = false;
    var moved = false;
    var startX = 0;
    var startY = 0;
    var origL = 0;
    var origT = 0;

    function onMove(clientX, clientY) {
      if (!dragging) return;
      var dx = clientX - startX;
      var dy = clientY - startY;
      if (Math.abs(dx) + Math.abs(dy) > 4) moved = true;
      applyFabPos(origL + dx, origT + dy);
    }

    function onUp() {
      if (!dragging) return;
      dragging = false;
      dock.classList.remove('is-dragging');
      document.removeEventListener('pointermove', onPointerMove);
      document.removeEventListener('pointerup', onPointerUp);
      document.removeEventListener('pointercancel', onPointerUp);
      if (moved) {
        var rect = dock.getBoundingClientRect();
        var pos = applyFabPos(rect.left, rect.top);
        try { localStorage.setItem(FAB_POS_KEY, JSON.stringify(pos)); } catch (_) {}
      }
    }

    function onPointerMove(ev) {
      onMove(ev.clientX, ev.clientY);
    }

    function onPointerUp() {
      onUp();
    }

    handle.addEventListener('pointerdown', function (ev) {
      if (ev.button != null && ev.button !== 0) return;
      var rect = dock.getBoundingClientRect();
      dragging = true;
      moved = false;
      startX = ev.clientX;
      startY = ev.clientY;
      origL = rect.left;
      origT = rect.top;
      dock.classList.add('is-dragging');
      try { handle.setPointerCapture(ev.pointerId); } catch (_) {}
      document.addEventListener('pointermove', onPointerMove);
      document.addEventListener('pointerup', onPointerUp);
      document.addEventListener('pointercancel', onPointerUp);
      ev.preventDefault();
    });

    handle.addEventListener('click', function (ev) {
      if (moved) {
        ev.preventDefault();
        ev.stopPropagation();
        moved = false;
        return;
      }
      runPublish({ direct: true });
    });
  }

  bindFabHover();
  bindFabDrag();
  // 展开侧栏时不必清位置；下次收起继续用
  var _setCollapsed = setSidebarCollapsed;
  setSidebarCollapsed = function (collapsed) {
    _setCollapsed(collapsed);
    if (collapsed) {
      // 下一帧再恢复，确保尺寸已渲染
      requestAnimationFrame(restoreFabPos);
    }
  };

  $('previewReloadBtn').addEventListener('click', function () {
    reloadPreviewOnly();
  });

  var fabExpand = $('fabExpand');
  if (fabExpand) {
    fabExpand.addEventListener('click', function () {
      setSidebarCollapsed(false);
    });
  }

  var fabReload = $('fabReload');
  if (fabReload) {
    fabReload.addEventListener('click', function () {
      reloadPreviewOnly();
    });
  }

  function setFabSyncState(mode, text) {
    var btn = $('fabSync');
    if (!btn) return;
    btn.classList.remove('is-run', 'is-ok', 'is-err');
    if (mode) btn.classList.add('is-' + mode);
    if (text) btn.textContent = text;
    else if (mode === 'run') btn.textContent = '…';
    else if (mode === 'ok') btn.textContent = '完成';
    else if (mode === 'err') btn.textContent = '失败';
    else btn.textContent = '同步';
  }

  var syncTimer = null;

  function stopSyncPoll() {
    if (syncTimer) {
      clearInterval(syncTimer);
      syncTimer = null;
    }
  }

  function applySyncJob(job) {
    if (!job) return;
    if (job.running) {
      var short = job.total ? (job.current + '/' + job.total) : '…';
      setFabSyncState('run', short.length > 6 ? '…' : short);
      var prog = job.total
        ? ('同步 ' + job.current + '/' + job.total)
        : (job.message || '同步中…');
      showMsg(prog, true);
      return;
    }
    if (job.done) {
      stopSyncPoll();
      setBusy(false);
      if (job.error || job.failCount) {
        setFabSyncState('err', '失败');
        showMsg(job.error || job.message || '同步失败', false);
        setTimeout(function () { setFabSyncState('', '同步'); }, 2200);
      } else {
        setFabSyncState('ok', '完成');
        showMsg(job.message || ('已同步 ' + (job.okCount || 0) + ' 个文件'), true);
        setTimeout(function () { setFabSyncState('', '同步'); }, 1600);
      }
    }
  }

  function startSyncPoll() {
    stopSyncPoll();
    var tick = async function () {
      try {
        var data = await api('/api/sync');
        applySyncJob(data.job);
      } catch (_) {}
    };
    tick();
    syncTimer = setInterval(tick, 1000);
  }

  async function runFabSync() {
    setBusy(true, { keepEnabled: { fabSync: true, fabExpand: true, fabReload: true } });
    setFabSyncState('run', '…');
    showMsg('正在启动同步…', true);
    try {
      var data = await api('/api/sync', {
        method: 'POST',
        body: JSON.stringify({
          characterId: $('characterId').value,
          projectPath: $('projectPath').value.trim(),
          message: 'sync from local console',
        }),
      });
      if (!data.ok) {
        setBusy(false);
        setFabSyncState('err', '失败');
        showMsg(data.error || '同步失败', false);
        setTimeout(function () { setFabSyncState('', '同步'); }, 2200);
        return;
      }
      applySyncJob(data.job);
      startSyncPoll();
    } catch (e) {
      setBusy(false);
      setFabSyncState('err', '失败');
      showMsg(String(e.message || e), false);
      setTimeout(function () { setFabSyncState('', '同步'); }, 2200);
    }
  }

  var fabSync = $('fabSync');
  if (fabSync) {
    fabSync.addEventListener('click', function () {
      runFabSync();
    });
  }

  async function startPullRequest(path, busyText, extraBody) {
    setBusy(true);
    showMsg(busyText, true);
    try {
      await api('/api/config', {
        method: 'POST',
        body: JSON.stringify({
          email: $('email').value.trim(),
          characterId: $('characterId').value,
          projectPath: $('projectPath').value.trim(),
          previewPort: $('previewPort').value,
        }),
      });
      var data = await api(path, {
        method: 'POST',
        body: JSON.stringify(Object.assign({
          characterId: $('characterId').value,
          projectPath: $('projectPath').value.trim(),
        }, extraBody || {})),
      });
      if (data.job) renderPull(data.job);
      if (data.status) fillForm(data.status);
      if (data.ok) {
        showMsg(busyText.replace('正在启动', '进行中').replace('正在', '') || '进行中…', true);
        startPullPoll();
      } else {
        showMsg(data.error || '无法开始', false);
        setBusy(false);
      }
    } catch (e) {
      showMsg(String(e.message || e), false);
      setBusy(false);
    }
  }

  $('pullBtn').addEventListener('click', async function () {
    if (!confirm('将从云端容器拉取完整项目到本地路径（覆盖同名文件）。继续？')) return;
    startPullRequest('/api/pull', '正在启动全量拉取…');
  });

  $('pullRetryBtn').addEventListener('click', async function () {
    var job = lastPullJob || {};
    var failed = job.failedPaths || [];
    if (!confirmRetryFailedPull(job)) return;
    showMsg('提醒：即将只重拉 ' + failed.length + ' 个失败文件', true);
    startPullRequest(
      '/api/pull/retry',
      '正在重拉 ' + failed.length + ' 个失败文件…',
      { failedPaths: failed }
    );
  });

  document.querySelectorAll('.mode-item').forEach(function (btn) {
    btn.addEventListener('click', function () {
      switchConsoleMode(btn.getAttribute('data-mode'));
    });
  });
  document.querySelectorAll('.menu-item').forEach(function (btn) {
    btn.addEventListener('click', function () {
      switchPanel(btn.getAttribute('data-panel'));
    });
  });

  ['cardName', 'cardDescription', 'cardPersonality', 'cardScenario', 'cardFirstMes', 'cardTags', 'cardNotes', 'cardBriefMain', 'cardBrief', 'cardSystemPrompt', 'cardCreator', 'cardVersion', 'cardBookName', 'cardSuggestedReplies', 'cardChatHistory'].forEach(function (id) {
    var el = $(id);
    if (!el) return;
    el.addEventListener('input', updateCardPreview);
  });
  updateCardTokenStats();

  document.querySelectorAll('.card-tab').forEach(function (btn) {
    btn.addEventListener('click', function () {
      var name = btn.getAttribute('data-card-tab');
      document.querySelectorAll('.card-tab').forEach(function (b) {
        b.classList.toggle('active', b === btn);
      });
      document.querySelectorAll('.card-tab-panel').forEach(function (panel) {
        var match = panel.getAttribute('data-card-panel') === name;
        panel.classList.toggle('active', match);
        if (match) panel.removeAttribute('hidden');
        else panel.setAttribute('hidden', '');
      });
    });
  });

  if ($('cardWbAddBtn')) {
    $('cardWbAddBtn').addEventListener('click', function () {
      var list = collectWorldBookEntries();
      list.push({
        id: list.length + 1,
        name: '条目' + (list.length + 1),
        keys: [],
        content: '',
        enabled: true,
        constant: false,
        insertion_order: list.length,
        position: 4,
        priority: 100,
        extensions: {},
      });
      renderWorldBookEntries(list);
      updateCardPreview();
    });
  }

  if ($('cardAvatarUrl')) {
    $('cardAvatarUrl').addEventListener('input', function () {
      updateAvatarPreview($('cardAvatarUrl').value);
    });
  }

  function currentCardLocalId() {
    return currentCardId
      || ($('cardFolderName') && $('cardFolderName').value.trim())
      || ($('cardName') && $('cardName').value.trim())
      || '';
  }

  function readFileAsDataUrl(file) {
    return new Promise(function (resolve, reject) {
      var reader = new FileReader();
      reader.onload = function () { resolve(String(reader.result || '')); };
      reader.onerror = function () { reject(new Error('读取文件失败')); };
      reader.readAsDataURL(file);
    });
  }

  async function importAvatarFile(file) {
    var localId = currentCardLocalId();
    if (!localId) throw new Error('请先创建/打开卡夹');
    if (!/^image\//.test(file.type || '')) throw new Error('请选择图片文件');
    if (file.size > 12 * 1024 * 1024) throw new Error('图片太大（上限 12MB）');
    var dataUrl = await readFileAsDataUrl(file);
    var data = await api('/api/card/avatar', {
      method: 'POST',
      body: JSON.stringify({
        localId: localId,
        filename: file.name || 'avatar.png',
        mime: file.type || '',
        dataBase64: dataUrl,
      }),
    });
    if (!data.ok) throw new Error(data.error || '导入失败');
    fillCardForm(data.card, data.localId || localId, { mtime: data.mtime || 0 });
    currentCardFolder = data.folder || data.path || currentCardFolder;
    await refreshCardList();
    showMsg('头像已保存 · ' + (data.rel || 'assets/avatar'), true);
  }

  async function importImageFiles(fileList) {
    var localId = currentCardLocalId();
    if (!localId) throw new Error('请先创建/打开卡夹');
    var files = Array.isArray(fileList) ? fileList.slice() : Array.prototype.slice.call(fileList || []);
    if (!files.length) throw new Error('未选择图片');
    var last = null;
    var imported = 0;
    for (var i = 0; i < files.length; i++) {
      var file = files[i];
      if (!file) continue;
      // 部分浏览器 type 为空，按扩展名放行
      var okType = /^image\//.test(file.type || '');
      var okExt = /\.(png|jpe?g|webp|gif)$/i.test(file.name || '');
      if (!okType && !okExt) continue;
      if (file.size > 12 * 1024 * 1024) throw new Error(file.name + ' 超过 12MB');
      showMsg('正在导入立绘 ' + (imported + 1) + '/' + files.length + ' · ' + (file.name || ''), true);
      var dataUrl = await readFileAsDataUrl(file);
      last = await api('/api/card/image', {
        method: 'POST',
        body: JSON.stringify({
          localId: localId,
          filename: file.name || ('image_' + i + '.png'),
          mime: file.type || '',
          name: (file.name || '').replace(/\.[^.]+$/, ''),
          dataBase64: dataUrl,
        }),
      });
      if (!last.ok) throw new Error(last.error || '立绘导入失败');
      imported += 1;
    }
    if (!last || !imported) throw new Error('没有可导入的图片（请选 png/jpg/webp/gif）');
    fillCardForm(last.card, last.localId || localId, { mtime: last.mtime || 0 });
    currentCardFolder = last.folder || last.path || currentCardFolder;
    await refreshCardList();
    showMsg('立绘已导入 ' + imported + ' 张 · 当前共 ' + ((last.card.data && last.card.data.image_info) || []).length + ' 张', true);
  }

  async function removeCardImage(index) {
    var localId = currentCardLocalId();
    if (!localId) {
      showMsg('请先打开卡夹', false);
      return;
    }
    setBusy(true);
    try {
      var data = await api('/api/card/image', {
        method: 'POST',
        body: JSON.stringify({ localId: localId, action: 'remove', index: index }),
      });
      if (!data.ok) {
        showMsg(data.error || '删除失败', false);
        return;
      }
      fillCardForm(data.card, data.localId || localId, { mtime: data.mtime || 0 });
      showMsg('已删除立绘 #' + (index + 1), true);
    } catch (e) {
      showMsg(String(e.message || e), false);
    } finally {
      setBusy(false);
    }
  }

  async function refreshVoiceCatalog() {
    setBusy(true);
    if ($('cardVoiceMsg')) $('cardVoiceMsg').textContent = '正在拉取平台音色库…';
    try {
      var data = await api('/api/card/voices');
      if (!data.ok) {
        if ($('cardVoiceMsg')) $('cardVoiceMsg').textContent = data.error || '拉取失败（需登录）';
        showMsg(data.error || '音色库拉取失败', false);
        return;
      }
      voiceCatalog = { public: data.public || [], mine: data.mine || [] };
      if ($('cardVoiceMsg')) {
        $('cardVoiceMsg').textContent =
          '公共 ' + voiceCatalog.public.length + ' · 我的 ' + voiceCatalog.mine.length +
          '（显示前 80 条筛选结果）';
      }
      renderVoiceList();
      showMsg('音色库已刷新', true);
    } catch (e) {
      showMsg(String(e.message || e), false);
    } finally {
      setBusy(false);
    }
  }

  async function selectVoice(v) {
    var localId = currentCardLocalId();
    if (!localId) {
      showMsg('请先打开卡夹再选音色', false);
      return;
    }
    setBusy(true);
    try {
      var data = await api('/api/card/voice', {
        method: 'POST',
        body: JSON.stringify({
          localId: localId,
          voice: {
            id: v.id,
            name: v.name,
            avatar_url: v.avatarUrl || null,
            preview_url: v.previewUrl || null,
            gender: v.gender || '',
          },
        }),
      });
      if (!data.ok) {
        showMsg(data.error || '选用失败', false);
        return;
      }
      fillCardForm(data.card, data.localId || localId, { mtime: data.mtime || 0 });
      showMsg('已选用音色 · ' + (v.name || v.id), true);
    } catch (e) {
      showMsg(String(e.message || e), false);
    } finally {
      setBusy(false);
    }
  }

  if ($('cardAvatarFile')) {
    $('cardAvatarFile').addEventListener('change', async function () {
      var file = $('cardAvatarFile').files && $('cardAvatarFile').files[0];
      $('cardAvatarFile').value = '';
      if (!file) return;
      setBusy(true);
      showMsg('正在导入本地头像…', true);
      try {
        await importAvatarFile(file);
      } catch (e) {
        showMsg(String(e.message || e), false);
      } finally {
        setBusy(false);
      }
    });
  }

  if ($('cardImageFile')) {
    $('cardImageFile').addEventListener('change', async function () {
      // 必须先拷成数组：清空 value 会让 FileList 立刻变空
      var files = Array.prototype.slice.call(($('cardImageFile').files) || []);
      $('cardImageFile').value = '';
      if (!files.length) return;
      setBusy(true);
      showMsg('正在导入立绘 ' + files.length + ' 张…', true);
      try {
        await importImageFiles(files);
      } catch (e) {
        showMsg(String(e.message || e), false);
      } finally {
        setBusy(false);
      }
    });
  }

  if ($('cardVoiceRefreshBtn')) {
    $('cardVoiceRefreshBtn').addEventListener('click', refreshVoiceCatalog);
  }
  if ($('cardVoiceClearBtn')) {
    $('cardVoiceClearBtn').addEventListener('click', async function () {
      var localId = currentCardLocalId();
      if (!localId) {
        showMsg('请先打开卡夹', false);
        return;
      }
      setBusy(true);
      try {
        var data = await api('/api/card/voice', {
          method: 'POST',
          body: JSON.stringify({ localId: localId, clear: true }),
        });
        if (!data.ok) {
          showMsg(data.error || '清除失败', false);
          return;
        }
        fillCardForm(data.card, data.localId || localId, { mtime: data.mtime || 0 });
        showMsg('已清除音色', true);
      } catch (e) {
        showMsg(String(e.message || e), false);
      } finally {
        setBusy(false);
      }
    });
  }
  if ($('cardVoiceSearch')) {
    $('cardVoiceSearch').addEventListener('input', renderVoiceList);
  }
  if ($('cardVoiceGender')) {
    $('cardVoiceGender').addEventListener('change', renderVoiceList);
  }

  function getCardBrief() {
    var main = ($('cardBriefMain') && $('cardBriefMain').value) || '';
    var side = ($('cardBrief') && $('cardBrief').value) || '';
    return (main || side).trim();
  }

  async function runCardAiWrite() {
    var name = ($('cardFolderName') && $('cardFolderName').value.trim())
      || ($('cardName') && $('cardName').value.trim())
      || '';
    var brief = getCardBrief();
    if (!name && brief.length < 2) {
      showMsg('请先填卡名（或创意简述）', false);
      setCardMsg('请先填卡名（或创意简述）', false);
      return;
    }
    if ($('cardBrief')) $('cardBrief').value = brief;
    if ($('cardBriefMain')) $('cardBriefMain').value = brief;
    setBusy(true);
    showMsg('正在创建本地卡夹…', true);
    setCardMsg('正在创建本地卡夹…', true);
    try {
      var data = await api('/api/card/ai', {
        method: 'POST',
        body: JSON.stringify({ name: name, brief: brief }),
      });
      if (!data.ok) {
        showMsg(data.error || '创建卡夹失败', false);
        setCardMsg(data.error || '创建卡夹失败', false);
        return;
      }
      fillCardForm(data.card, data.localId || '', { mtime: data.mtime || 0 });
      currentCardFolder = data.folder || data.path || '';
      if ($('cardFolderName')) $('cardFolderName').value = data.localId || name;
      setStageMode('card');
      setCardFullscreen(true);
      await refreshCardList();
      // 创建并监听后：自动复制「写这张卡」的开聊提示词
      var copied = await runCopyChatPrompt({
        name: data.localId || name,
        brief: brief,
        auto: true,
      });
      if (!copied) {
        var fallback = '已监听 · ' + (data.localId || currentCardFolder)
          + '（开聊提示词未复制成功，可点「复制开聊提示词」）';
        showMsg(fallback, true);
        setCardMsg(fallback, true);
      }
    } catch (e) {
      showMsg(String(e.message || e), false);
      setCardMsg(String(e.message || e), false);
    } finally {
      setBusy(false);
    }
  }

  async function runCardNew() {
    var name = window.prompt('请输入新卡名称（必填）', '');
    if (name === null) return; // 取消
    name = String(name || '').trim();
    if (!name) {
      showMsg('必须填写卡名才能新建', false);
      return;
    }
    setBusy(true);
    try {
      // 空白新建：用弹窗卡名，简述清空，不沿用上一张
      if ($('cardBrief')) $('cardBrief').value = '';
      if ($('cardBriefMain')) $('cardBriefMain').value = '';
      var data = await api('/api/card/new', {
        method: 'POST',
        body: JSON.stringify({ name: name, brief: '', blank: true }),
      });
      if (!data.ok) {
        showMsg(data.error || '新建失败', false);
        return;
      }
      fillCardForm(data.card, data.localId, { mtime: data.mtime || 0 });
      currentCardFolder = data.folder || data.path || '';
      if ($('cardName')) $('cardName').value = name;
      if ($('cardFolderName')) $('cardFolderName').value = data.localId || name;
      if ($('cardBrief')) $('cardBrief').value = '';
      if ($('cardBriefMain')) $('cardBriefMain').value = '';
      setStageMode('card');
      setCardFullscreen(true);
      await refreshCardList();
      showMsg('已新建空白卡夹 · ' + (data.folder || data.localId), true);
    } catch (e) {
      showMsg(String(e.message || e), false);
    } finally {
      setBusy(false);
    }
  }

  async function copyTextToClipboard(text) {
    if (navigator.clipboard && window.isSecureContext) {
      try {
        await navigator.clipboard.writeText(text);
        return;
      } catch (_) {
        /* fall through to execCommand */
      }
    }
    var ta = document.createElement('textarea');
    ta.value = text;
    ta.setAttribute('readonly', '');
    ta.style.position = 'fixed';
    ta.style.left = '-9999px';
    document.body.appendChild(ta);
    ta.focus();
    ta.select();
    ta.setSelectionRange(0, ta.value.length);
    var ok = false;
    try {
      ok = document.execCommand('copy');
    } finally {
      document.body.removeChild(ta);
    }
    if (!ok) throw new Error('浏览器拒绝写入剪贴板，请手动复制');
  }

  function resolveChatPromptContext(opts) {
    opts = opts || {};
    var name = (opts.name || '').trim()
      || currentCardId
      || ($('cardFolderName') && $('cardFolderName').value.trim())
      || ($('cardName') && $('cardName').value.trim())
      || '';
    var brief = (opts.brief != null ? String(opts.brief) : getCardBrief()).trim();
    if (!brief && currentCard && currentCard._meta && currentCard._meta.brief) {
      brief = String(currentCard._meta.brief || '').trim();
    }
    return { name: name, brief: brief };
  }

  async function runCopyChatPrompt(opts) {
    opts = opts || {};
    var ctx = resolveChatPromptContext(opts);
    var name = ctx.name;
    var brief = ctx.brief;
    var manageBusy = !opts.auto;
    if (manageBusy) setBusy(true);
    setCardMsg('正在生成「' + (name || '未命名') + '」开聊提示词…', true);
    try {
      if (!name) {
        var needName = '请先填写卡名，或点「创建卡夹并监听」后再复制';
        setCardMsg(needName, false);
        showMsg(needName, false);
        return false;
      }
      var q = '/api/card/chat-prompt?name=' + encodeURIComponent(name)
        + '&brief=' + encodeURIComponent(brief);
      var data = await api(q);
      if (!data.ok || !data.text) {
        var err = data.error || '读取开聊提示词失败';
        if (err === '无效响应' || /HTTP 404/i.test(err)) {
          err = '开聊提示词接口不可用，请重启控制台（python console.py）后重试';
        }
        setCardMsg(err, false);
        showMsg(err, false);
        return false;
      }
      await copyTextToClipboard(data.text);
      var hint = opts.auto
        ? ('已创建并监听「' + name + '」，开聊提示词已复制 · 粘贴到新对话即可开始写这张卡')
        : ('已复制开聊提示词 · 创作「' + name + '」');
      if (!opts.auto && data.filledBrief) hint += '（含简述）';
      setCardMsg(hint, true);
      showMsg(hint, true);
      return true;
    } catch (e) {
      var msg = String(e.message || e);
      setCardMsg(msg, false);
      showMsg(msg, false);
      return false;
    } finally {
      if (manageBusy) setBusy(false);
    }
  }

  if ($('cardAiBtn')) $('cardAiBtn').addEventListener('click', runCardAiWrite);
  if ($('cardCopyPromptBtn')) $('cardCopyPromptBtn').addEventListener('click', function () { runCopyChatPrompt(); });
  if ($('cardNewBtn2')) $('cardNewBtn2').addEventListener('click', runCardNew);
  if ($('cardMenuBtn')) {
    $('cardMenuBtn').addEventListener('click', function () {
      setSidebarCollapsed(false);
    });
  }
  if ($('cardBrief') && $('cardBriefMain')) {
    $('cardBrief').addEventListener('input', function () {
      $('cardBriefMain').value = $('cardBrief').value;
    });
    $('cardBriefMain').addEventListener('input', function () {
      $('cardBrief').value = $('cardBriefMain').value;
    });
  }

  if ($('cardSaveBtn')) {
    $('cardSaveBtn').addEventListener('click', function () { saveCurrentCard(); });
  }
  if ($('cardExportPngBtn')) {
    $('cardExportPngBtn').addEventListener('click', async function () {
      var localId = currentCardLocalId();
      if (!localId) {
        showMsg('请先打开或保存一张本地卡', false);
        return;
      }
      setBusy(true);
      showMsg('正在打包 PNG 卡（封面=第一张图）…', true);
      try {
        // 先落盘，保证导出内容最新
        await saveCurrentCard();
        localId = currentCardLocalId() || localId;
        var a = document.createElement('a');
        a.href = '/api/card/export-png?id=' + encodeURIComponent(localId) + '&t=' + Date.now();
        a.download = localId + '.png';
        document.body.appendChild(a);
        a.click();
        a.remove();
        showMsg('已导出 PNG 卡 · 卡/' + localId + '/export/', true);
      } catch (e) {
        showMsg(String(e.message || e), false);
      } finally {
        setBusy(false);
      }
    });
  }

  function cardLooksEmpty(card) {
    if (!card || !card.data) return true;
    var d = card.data;
    var text = [
      d.description, d.personality, d.scenario, d.system_prompt, d.first_mes, d.creator_notes
    ].join('');
    var hasImg = !!(d.avatar_url || (d.image_info && d.image_info.length));
    return text.replace(/\s+/g, '').length < 20 && !hasImg;
  }

  async function saveToCloud(asDraft) {
    var localId = currentCardLocalId();
    if (!localId) {
      showMsg('请先打开/创建卡夹', false);
      return;
    }
    var card = collectCardFromForm();
    if (cardLooksEmpty(card)) {
      showMsg('表单几乎为空，已取消，避免把本地卡写空。请先「重载当前」或打开卡夹。', false);
      return;
    }
    setBusy(true);
    showMsg(asDraft ? '正在保存云端草稿…' : '正在保存到云端（含图片上传）…', true);
    try {
      var savedLocal = await api('/api/card/save', {
        method: 'POST',
        body: JSON.stringify({
          localId: localId,
          brief: getCardBrief(),
          card: card,
        }),
      });
      if (!savedLocal.ok) {
        showMsg(savedLocal.error || '本地保存失败', false);
        return;
      }
      fillCardForm(savedLocal.card, savedLocal.localId || localId, { mtime: savedLocal.mtime || 0 });
      localId = savedLocal.localId || localId;

      var data = await api('/api/card/publish', {
        method: 'POST',
        body: JSON.stringify({
          localId: localId,
          draft: !!asDraft,
          brief: getCardBrief(),
        }),
      });
      if (!data.ok) {
        showMsg(data.error || (asDraft ? '云端草稿失败' : '云端保存失败'), false);
        return;
      }
      fillCardForm(data.card, data.localId || localId, { mtime: data.mtime || 0 });
      currentCardFolder = data.folder || data.path || currentCardFolder;
      await refreshCardList();
      if (asDraft || data.mode === 'draft') {
        showMsg('草稿已保存 · id=' + (data.cloudId || ''), true);
      } else {
        var draftId = data.syncedDraftId || (data.draftSync && data.draftSync.draftId) || '';
        var draftHint = draftId
          ? (' · 已同步创作草稿 #' + draftId + '（官网列表看的是这份）')
          : '';
        showMsg(
          '已保存正式卡 · #' + data.cloudId + draftHint + '（上架请去官网）' +
            (data.characterUrl ? (' · ' + data.characterUrl) : ''),
          true
        );
      }
      updatePlayGate();
      refreshCloudCardList(false);
    } catch (e) {
      showMsg(String(e.message || e), false);
    } finally {
      setBusy(false);
    }
  }

  if ($('cardPublishBtn')) $('cardPublishBtn').addEventListener('click', function () { saveToCloud(false); });
  if ($('cardDraftBtn')) $('cardDraftBtn').addEventListener('click', function () { saveToCloud(true); });

  function currentCloudCardId() {
    if (!currentCard) return 0;
    var d = currentCard.data || {};
    var meta = currentCard._meta || {};
    if (meta.source === 'cloud-draft' || meta.isDraft) return 0;
    var id = parseInt(d.db_id || meta.cloudId || 0, 10);
    return id > 0 ? id : 0;
  }

  function updatePlayGate() {
    var btn = $('cardPlayBtn');
    if (!btn) return;
    var cid = currentCloudCardId();
    btn.disabled = !cid || cardPlayMode;
    btn.title = cid
      ? ('试玩云端卡 #' + cid)
      : '需先「保存到云端」得到正式卡 ID（草稿不可试玩）';
  }

  function setCardPlayMode(on) {
    cardPlayMode = !!on;
    var stage = $('cardStage');
    if (stage) stage.classList.toggle('is-play', cardPlayMode);
    if ($('cardForm')) {
      $('cardForm').hidden = cardPlayMode;
      // 双保险：退出试玩时清掉可能残留的内联样式
      if (!cardPlayMode) $('cardForm').style.removeProperty('display');
    }
    if ($('cardPlayPanel')) $('cardPlayPanel').hidden = !cardPlayMode;
    // 试玩时顶部只保留菜单 + 返回写卡；写卡界面绝不显示返回写卡
    ['cardNewBtn2', 'cardSaveBtn', 'cardExportPngBtn', 'cardPublishBtn', 'cardDraftBtn', 'cardPlayBtn', 'cardReloadBtn'].forEach(function (id) {
      var el = $(id);
      if (!el) return;
      if (cardPlayMode) el.setAttribute('hidden', '');
      else el.removeAttribute('hidden');
    });
    if ($('cardPlayBackBtn')) {
      if (cardPlayMode) $('cardPlayBackBtn').removeAttribute('hidden');
      else $('cardPlayBackBtn').setAttribute('hidden', '');
    }
    if ($('cardStageTitle')) $('cardStageTitle').textContent = cardPlayMode ? '角色卡试玩' : '角色卡编辑';
    updatePlayGate();
  }

  async function exitCardPlay(opts) {
    opts = opts || {};
    var chatId = playState.chatId || '';
    // 返回写卡 / 结束试玩：默认调平台 chat.deleteChat 删掉本次会话
    var doDelete = !!chatId && (opts.deleteChat === true || (!opts.keepSession && opts.deleteChat !== false));
    if (doDelete) {
      try {
        var del = await api('/api/card/play/delete', {
          method: 'POST',
          body: JSON.stringify({ chatId: chatId }),
        });
        if (del && del.ok) {
          showMsg('已删除试玩会话', true);
        } else {
          showMsg((del && del.error) || '删除试玩会话失败（仍返回写卡）', false);
        }
      } catch (e) {
        showMsg(String(e.message || e) || '删除试玩会话失败（仍返回写卡）', false);
      }
    }
    if (!opts.keepSession || doDelete) {
      playState.chatId = '';
      playState.messages = [];
      playState.sending = false;
      playState.charName = '';
      if ($('playMessages')) $('playMessages').innerHTML = '';
    }
    if (!opts.keepPanel) setCardPlayMode(false);
    if ($('playStatus')) $('playStatus').textContent = playState.chatId ? ('会话 ' + playState.chatId.slice(0, 8) + '…') : '未开始';
  }

  function formatContextLabel(maxContext) {
    var n = Number(maxContext) || 0;
    if (n >= 1000) return Math.round(n / 1000) + 'K';
    return String(n || '');
  }

  /** 官网：模型按 series 分组，上下文长度 = 同组 contexts[] 不同 internalName */
  function parseModelGroups(modelsPayload) {
    var root = modelsPayload || {};
    var cats = root.categories || [];
    var groups = [];
    var seen = {};
    if (Array.isArray(cats)) {
      cats.forEach(function (c) {
        var catName = (c && (c.name || c.title || c.key)) || '';
        var list = (c && c.modelGroups) || [];
        if (!Array.isArray(list)) return;
        list.forEach(function (g, idx) {
          var contexts = Array.isArray(g.contexts) ? g.contexts.filter(function (x) {
            return x && (x.internalName || x.id);
          }) : [];
          if (!contexts.length) return;
          var series = String(g.seriesKey || g.displayName || contexts[0].displayName || ('model-' + idx));
          var key = String(g.categoryKey || catName || 'cat') + '::' + series;
          if (seen[key]) return;
          seen[key] = 1;
          groups.push({
            key: key,
            seriesKey: series,
            category: catName,
            thinkingSupported: !!g.thinkingSupported,
            contexts: contexts.map(function (ctx) {
              return {
                id: String(ctx.internalName || ctx.id),
                label: formatContextLabel(ctx.maxContext) || String(ctx.displayName || ctx.internalName),
                maxContext: Number(ctx.maxContext) || 0,
                displayName: ctx.displayName || '',
                isRecommended: !!ctx.isRecommended,
              };
            }),
          });
        });
      });
    }
    return { groups: groups, defaultModel: root.defaultModel || '' };
  }

  function findPlayGroup(key) {
    var list = playState.modelGroups || [];
    for (var i = 0; i < list.length; i++) {
      if (list[i].key === key) return list[i];
    }
    return null;
  }

  function findGroupByModelId(modelId) {
    var id = String(modelId || '');
    var list = playState.modelGroups || [];
    for (var i = 0; i < list.length; i++) {
      var g = list[i];
      for (var j = 0; j < g.contexts.length; j++) {
        if (g.contexts[j].id === id) return g;
      }
    }
    return null;
  }

  function pickContextForGroup(group, preferId) {
    if (!group || !group.contexts.length) return null;
    if (preferId) {
      for (var i = 0; i < group.contexts.length; i++) {
        if (group.contexts[i].id === preferId) return group.contexts[i];
      }
    }
    // 官网默认偏 16K（推荐）；否则取最小上下文
    var rec = null;
    var smallest = group.contexts[0];
    group.contexts.forEach(function (c) {
      if (c.isRecommended && (!rec || c.maxContext < rec.maxContext)) rec = c;
      if (c.maxContext && c.maxContext < (smallest.maxContext || Infinity)) smallest = c;
    });
    return rec || smallest || group.contexts[0];
  }

  function renderPlayContextPills() {
    var wrap = $('playContextLength');
    if (!wrap) return;
    wrap.innerHTML = '';
    var group = findPlayGroup(playState.selectedGroupKey);
    if (!group) {
      wrap.textContent = '—';
      return;
    }
    group.contexts.forEach(function (ctx) {
      var btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'play-context-pill' + (ctx.id === playState.selectedModel ? ' is-active' : '');
      btn.textContent = ctx.label;
      btn.title = ctx.displayName || ctx.id;
      btn.addEventListener('click', function () {
        playState.selectedModel = ctx.id;
        renderPlayContextPills();
        if (playState.chatId) {
          patchPlaySettings({ model: ctx.id }).catch(function () {});
        }
      });
      wrap.appendChild(btn);
    });
  }

  function populatePlayModelSelect(parsed) {
    var sel = $('playModelSelect');
    if (!sel) return;
    playState.modelGroups = parsed.groups || [];
    playState.defaultModel = parsed.defaultModel || '';
    sel.innerHTML = '';
    if (!playState.modelGroups.length) {
      sel.innerHTML = '<option value="">（无可用模型）</option>';
      playState.selectedGroupKey = '';
      playState.selectedModel = '';
      renderPlayContextPills();
      return;
    }
    playState.modelGroups.forEach(function (g) {
      var opt = document.createElement('option');
      opt.value = g.key;
      opt.textContent = g.category ? (g.seriesKey + ' · ' + g.category) : g.seriesKey;
      sel.appendChild(opt);
    });
    var group = findGroupByModelId(playState.selectedModel || playState.defaultModel)
      || findPlayGroup(playState.selectedGroupKey)
      || playState.modelGroups[0];
    playState.selectedGroupKey = group.key;
    sel.value = group.key;
    var ctx = pickContextForGroup(group, playState.selectedModel || playState.defaultModel);
    playState.selectedModel = ctx ? ctx.id : '';
    renderPlayContextPills();
  }

  function onPlayModelGroupChange() {
    var sel = $('playModelSelect');
    if (!sel) return;
    var group = findPlayGroup(sel.value);
    if (!group) return;
    playState.selectedGroupKey = group.key;
    // 切换系列时尽量保留同档上下文（如仍选 16K）
    var prev = null;
    var old = findGroupByModelId(playState.selectedModel);
    if (old) {
      for (var i = 0; i < old.contexts.length; i++) {
        if (old.contexts[i].id === playState.selectedModel) {
          prev = old.contexts[i].maxContext;
          break;
        }
      }
    }
    var matched = null;
    if (prev) {
      for (var j = 0; j < group.contexts.length; j++) {
        if (group.contexts[j].maxContext === prev) {
          matched = group.contexts[j];
          break;
        }
      }
    }
    var ctx = matched || pickContextForGroup(group, null);
    playState.selectedModel = ctx ? ctx.id : '';
    renderPlayContextPills();
    if (playState.chatId && playState.selectedModel) {
      patchPlaySettings({ model: playState.selectedModel }).catch(function () {});
    }
  }

  async function loadPlayControls() {
    if ($('playStatus')) $('playStatus').textContent = '加载模型/预设…';
    try {
      var modelsRes = await api('/api/card/play/models');
      if (modelsRes.ok) {
        populatePlayModelSelect(parseModelGroups(modelsRes.models || {}));
        playState.modelsLoaded = true;
      }
    } catch (e) {
      if ($('playModelSelect')) {
        $('playModelSelect').innerHTML = '<option value="">模型加载失败</option>';
      }
    }
    try {
      var pre = await api('/api/card/play/presets');
      var list = (pre && pre.presets) || [];
      var active = (pre && pre.activePresetIds) || [];
      playState.presetCatalog = Array.isArray(list) ? list.slice() : [];
      if (!playState.selectedPresetIds.length && Array.isArray(active) && active.length) {
        playState.selectedPresetIds = active.map(String);
      }
      playState.presetsLoaded = true;
      // 官网规则：{{user}} = 登录用户 fullName（非邮箱、非手动填）
      var dn = (pre && pre.displayName) || '';
      if (dn) playState.userName = String(dn).trim();
      // playerInfo 来自账号预设 settings，不是显示名
      playState.playerInfo = String((pre && pre.playerInfo) || playState.playerInfo || '').trim();
      updatePlayPresetBtn();
      updatePlayUserLabel();
      if (cardPlayMode) renderPlayMessages();
    } catch (_) {}
    if ($('playStatus')) {
      $('playStatus').textContent = playState.chatId
        ? ('会话 ' + String(playState.chatId).slice(0, 8) + '…')
        : '就绪';
    }
  }

  function selectedPresetIds() {
    return (playState.selectedPresetIds || []).slice();
  }

  function updatePlayPresetBtn() {
    var btn = $('playPresetBtn');
    if (!btn) return;
    var n = selectedPresetIds().length;
    btn.textContent = n ? ('预设 · ' + n) : '预设';
  }

  function updatePlayUserLabel() {
    var el = $('playUserLabel');
    if (!el) return;
    var name = playUserName();
    el.textContent = '{{user}} · ' + name;
    el.title = '开场白与显示中的 {{user}} = 登录用户名（' + name + '）';
  }

  function openPlayPresetModal() {
    var modal = $('playPresetModal');
    var list = $('playPresetList');
    if (!modal || !list) return;
    list.innerHTML = '';
    var catalog = playState.presetCatalog || [];
    var selected = {};
    selectedPresetIds().forEach(function (id) { selected[id] = 1; });
    if (!catalog.length) {
      var empty = document.createElement('p');
      empty.className = 'hint';
      empty.textContent = '账号暂无预设';
      list.appendChild(empty);
    } else {
      catalog.forEach(function (p) {
        var id = String(p.id || '');
        if (!id) return;
        var label = document.createElement('label');
        label.className = 'play-preset-item';
        var cb = document.createElement('input');
        cb.type = 'checkbox';
        cb.value = id;
        cb.checked = !!selected[id];
        var body = document.createElement('span');
        var name = document.createElement('span');
        name.className = 'play-preset-name';
        name.textContent = p.name || p.title || id;
        body.appendChild(name);
        var desc = p.description || p.desc || '';
        if (desc) {
          var d = document.createElement('span');
          d.className = 'play-preset-desc';
          d.textContent = String(desc).slice(0, 80);
          body.appendChild(d);
        }
        label.appendChild(cb);
        label.appendChild(body);
        list.appendChild(label);
      });
    }
    modal.hidden = false;
  }

  function closePlayPresetModal() {
    if ($('playPresetModal')) $('playPresetModal').hidden = true;
  }

  function applyPlayPresetModal() {
    var list = $('playPresetList');
    if (!list) return;
    var ids = [];
    list.querySelectorAll('input[type="checkbox"]').forEach(function (cb) {
      if (cb.checked && cb.value) ids.push(cb.value);
    });
    playState.selectedPresetIds = ids;
    updatePlayPresetBtn();
    closePlayPresetModal();
  }

  function playCharName() {
    return (
      playState.charName ||
      ($('cardName') && $('cardName').value.trim()) ||
      ($('cardFolderName') && $('cardFolderName').value.trim()) ||
      '角色'
    );
  }

  function playUserName() {
    return (playState.userName || '').trim() || '用户';
  }

  function applyPlayMacros(text) {
    var s = text == null ? '' : String(text);
    if (!s) return s;
    var user = playUserName();
    var char = playCharName();
    return s
      .replace(/\{\{user\}\}/gi, user)
      .replace(/\{\{char\}\}/gi, char)
      .replace(/<USER>/gi, user)
      .replace(/<BOT>/gi, char)
      .replace(/<CHAR>/gi, char);
  }

  function appendHighlightedText(parent, text) {
    var s = text == null ? '' : String(text);
    if (!s) return;
    if (!playState.enableHighlight) {
      parent.appendChild(document.createTextNode(s));
      return;
    }
    // 高亮中文引号内重点（官网「开启高亮」同类本地显示）
    var re = /([「『])([^」』]+)([」』])/g;
    var last = 0;
    var m;
    while ((m = re.exec(s)) !== null) {
      if (m.index > last) parent.appendChild(document.createTextNode(s.slice(last, m.index)));
      parent.appendChild(document.createTextNode(m[1]));
      var mark = document.createElement('mark');
      mark.className = 'play-hl';
      mark.textContent = m[2];
      parent.appendChild(mark);
      parent.appendChild(document.createTextNode(m[3]));
      last = m.index + m[0].length;
    }
    if (last < s.length) parent.appendChild(document.createTextNode(s.slice(last)));
  }

  /** 角色卡惯例：*旁白* → 斜体显示（不露出星号）；**粗体** 同理 */
  function appendPlayFormatted(parent, text) {
    var s = text == null ? '' : String(text);
    if (!s) return;
    var re = /\*\*([^*]+)\*\*|\*([^*\n]+)\*/g;
    var last = 0;
    var m;
    while ((m = re.exec(s)) !== null) {
      if (m.index > last) {
        appendHighlightedText(parent, s.slice(last, m.index));
      }
      if (m[1] != null) {
        var strong = document.createElement('strong');
        if (playState.enableHighlight) strong.className = 'play-hl';
        strong.textContent = m[1];
        parent.appendChild(strong);
      } else {
        var em = document.createElement('em');
        em.className = 'play-narration';
        em.textContent = m[2];
        parent.appendChild(em);
      }
      last = m.index + m[0].length;
    }
    if (last < s.length) {
      appendHighlightedText(parent, s.slice(last));
    }
  }

  function readLocalPlayPrefs() {
    try {
      playState.enableHighlight = localStorage.getItem('chat_enable_highlight') === 'true';
    } catch (_) {
      playState.enableHighlight = false;
    }
    try {
      playState.classicUi = localStorage.getItem('dzmm.playClassicUi') === 'true';
    } catch (_) {
      playState.classicUi = false;
    }
  }

  function applyPlayPanelChrome() {
    var panel = $('cardPlayPanel');
    if (!panel) return;
    panel.classList.toggle('is-highlight', !!playState.enableHighlight);
    panel.classList.toggle('is-classic', !!playState.classicUi);
  }

  function applyPlayChatSettings(settings) {
    if (!settings || typeof settings !== 'object') return;
    if (settings.title != null) playState.title = String(settings.title || '会话');
    if (settings.style) playState.style = String(settings.style);
    if (settings.maxTokens != null && settings.maxTokens !== '') {
      var mt = parseInt(settings.maxTokens, 10);
      if (!isNaN(mt)) playState.maxTokens = mt;
    }
    if (settings.imageGenerationModel) {
      playState.imageGenerationModel = String(settings.imageGenerationModel);
    }
    if (settings.model) {
      playState.selectedModel = String(settings.model);
      var g = findGroupByModelId(playState.selectedModel);
      if (g) {
        playState.selectedGroupKey = g.key;
        if ($('playModelSelect')) $('playModelSelect').value = g.key;
        renderPlayContextPills();
      }
    }
    if ($('playDeepThinking') && typeof settings.deepThinking === 'boolean') {
      $('playDeepThinking').checked = settings.deepThinking;
    }
    if ($('playMemoryEnhance') && typeof settings.enableMemoryEnhance === 'boolean') {
      $('playMemoryEnhance').checked = settings.enableMemoryEnhance;
    }
    applyPlayPanelChrome();
  }

  async function patchPlaySettings(partial) {
    if (!playState.chatId || !partial || typeof partial !== 'object') return null;
    var res = await api('/api/card/play/settings', {
      method: 'POST',
      body: JSON.stringify({ chatId: playState.chatId, settings: partial }),
    });
    if (res && res.ok && res.settings) {
      applyPlayChatSettings(res.settings);
    }
    return res;
  }

  function fillPlaySettingsForm() {
    if ($('playSetHighlight')) $('playSetHighlight').checked = !!playState.enableHighlight;
    if ($('playSetClassic')) $('playSetClassic').checked = !!playState.classicUi;
    if ($('playSetTitle')) $('playSetTitle').value = playState.title || '会话';
    if ($('playSetStyle')) $('playSetStyle').value = playState.style || 'standard';
    if ($('playSetMaxTokens')) $('playSetMaxTokens').value = String(playState.maxTokens || 2500);
    if ($('playSetImageModel')) {
      $('playSetImageModel').value = playState.imageGenerationModel || 'anime';
    }
  }

  function openPlaySettingsModal() {
    fillPlaySettingsForm();
    if ($('playSettingsModal')) $('playSettingsModal').hidden = false;
  }

  function closePlaySettingsModal() {
    if ($('playSettingsModal')) $('playSettingsModal').hidden = true;
  }

  async function savePlaySettingsModal() {
    var highlight = !!($('playSetHighlight') && $('playSetHighlight').checked);
    var classic = !!($('playSetClassic') && $('playSetClassic').checked);
    playState.enableHighlight = highlight;
    playState.classicUi = classic;
    try {
      localStorage.setItem('chat_enable_highlight', highlight ? 'true' : 'false');
      localStorage.setItem('dzmm.playClassicUi', classic ? 'true' : 'false');
    } catch (_) {}
    applyPlayPanelChrome();

    var title = ($('playSetTitle') && $('playSetTitle').value.trim()) || '会话';
    var style = ($('playSetStyle') && $('playSetStyle').value) || 'standard';
    var maxTokens = parseInt(($('playSetMaxTokens') && $('playSetMaxTokens').value) || '2500', 10);
    if (isNaN(maxTokens) || maxTokens < 0) maxTokens = 2500;
    var imageModel = ($('playSetImageModel') && $('playSetImageModel').value) || 'anime';

    playState.title = title;
    playState.style = style;
    playState.maxTokens = maxTokens;
    playState.imageGenerationModel = imageModel;

    if (playState.chatId) {
      setBusy(true);
      try {
        var res = await patchPlaySettings({
          title: title,
          style: style,
          maxTokens: maxTokens,
          imageGenerationModel: imageModel,
        });
        if (!res || !res.ok) {
          showMsg((res && res.error) || '设置保存失败', false);
          return;
        }
        showMsg('会话设置已保存', true);
      } catch (e) {
        showMsg(String(e.message || e), false);
        return;
      } finally {
        setBusy(false);
      }
    }
    renderPlayMessages();
    closePlaySettingsModal();
  }

  function renderPlayMessages() {
    var box = $('playMessages');
    if (!box) return;
    box.innerHTML = '';
    var charLabel = playCharName();
    var userLabel = playUserName();
    (playState.messages || []).forEach(function (m) {
      var div = document.createElement('div');
      var role = m.role === 'user' ? 'user' : 'assistant';
      div.className = 'play-msg ' + role + (m.streaming ? ' streaming' : '');
      var label = document.createElement('span');
      label.className = 'play-role';
      label.textContent = role === 'user' ? userLabel : charLabel;
      div.appendChild(label);
      var body = document.createElement('div');
      body.className = 'play-msg-body';
      appendPlayFormatted(body, applyPlayMacros(m.content || ''));
      div.appendChild(body);
      box.appendChild(div);
    });
    box.scrollTop = box.scrollHeight;
  }

  function renderPlaySuggest(replies) {
    var wrap = $('playSuggest');
    if (!wrap) return;
    wrap.innerHTML = '';
    var list = Array.isArray(replies) ? replies : [];
    if (!list.length) {
      wrap.hidden = true;
      return;
    }
    wrap.hidden = false;
    list.slice(0, 5).forEach(function (r) {
      var text = typeof r === 'string' ? r : (r && (r.content || r.text)) || '';
      if (!text) return;
      var shown = applyPlayMacros(text);
      var b = document.createElement('button');
      b.type = 'button';
      b.textContent = shown;
      b.addEventListener('click', function () {
        if ($('playInput')) $('playInput').value = shown;
        sendPlayMessage();
      });
      wrap.appendChild(b);
    });
  }

  async function enterCardPlay() {
    var cardId = currentCloudCardId();
    if (!cardId) {
      showMsg('请先「保存到云端」得到正式卡，再试玩（草稿不可用）', false);
      return;
    }
    setBusy(true);
    showMsg('正在创建平台会话…', true);
    try {
      setCardPlayMode(true);
      readLocalPlayPrefs();
      applyPlayPanelChrome();
      await loadPlayControls();
      if (!playState.charName) {
        playState.charName =
          ($('cardName') && $('cardName').value.trim()) ||
          ($('cardFolderName') && $('cardFolderName').value.trim()) ||
          '';
      }
      updatePlayUserLabel();
      // 同卡续聊
      if (playState.chatId && playState.cardId === cardId) {
        try {
          var contSet = await api('/api/card/play/settings?chatId=' + encodeURIComponent(playState.chatId));
          if (contSet.ok) applyPlayChatSettings(contSet.settings || {});
        } catch (_) {}
        renderPlayMessages();
        if ($('playStatus')) $('playStatus').textContent = '继续会话';
        showMsg('已进入试玩（沿用会话）', true);
        return;
      }
      var meta = await api('/api/card/play/meta?cardId=' + encodeURIComponent(cardId));
      var historyIndex = null;
      if (meta.ok && meta.preview && meta.preview.chatHistoryIndex != null) {
        historyIndex = meta.preview.chatHistoryIndex;
      }
      playState.charName =
        (meta.ok && meta.forChat && meta.forChat.name) ||
        ($('cardName') && $('cardName').value.trim()) ||
        ($('cardFolderName') && $('cardFolderName').value.trim()) ||
        '';
      updatePlayUserLabel();
      var start = await api('/api/card/play/start', {
        method: 'POST',
        body: JSON.stringify({ cardId: cardId, chatHistoryIndex: historyIndex }),
      });
      if (!start.ok) {
        setCardPlayMode(false);
        showMsg(start.error || '创建会话失败', false);
        return;
      }
      playState.chatId = start.chatId;
      playState.cardId = cardId;
      playState.messages = Array.isArray(start.messages) ? start.messages.slice() : [];
      if (start.settings) applyPlayChatSettings(start.settings);
      // 若会话尚无消息，用 quick preview 开场
      if (!playState.messages.length && meta.ok && meta.preview && meta.preview.firstMessage) {
        playState.messages.push({ role: 'assistant', content: meta.preview.firstMessage });
      }
      renderPlayMessages();
      renderPlaySuggest(
        (meta.ok && meta.preview && meta.preview.suggestedReplies) ||
        (meta.ok && meta.forChat && meta.forChat.suggestedReplies) ||
        []
      );
      if ($('playStatus')) $('playStatus').textContent = '会话已创建';
      showMsg('试玩已开始 · chat=' + String(start.chatId).slice(0, 10) + '…', true);
    } catch (e) {
      setCardPlayMode(false);
      showMsg(String(e.message || e), false);
    } finally {
      setBusy(false);
      updatePlayGate();
    }
  }

  function parsePlaySseBuffer(buf, onEvent) {
    var parts = buf.split('\n');
    var rest = parts.pop() || '';
    parts.forEach(function (line) {
      line = line.replace(/\r$/, '');
      if (!line.startsWith('data: ')) return;
      try {
        var obj = JSON.parse(line.slice(6));
        onEvent(obj);
      } catch (_) {}
    });
    return rest;
  }

  async function sendPlayMessage(textOverride) {
    if (playState.sending) return;
    var text = (textOverride != null ? textOverride : (($('playInput') && $('playInput').value) || '')).trim();
    if (!text) return;
    if (!playState.chatId || !playState.cardId) {
      showMsg('会话未就绪，请重新点试玩', false);
      return;
    }
    playState.sending = true;
    if ($('playSendBtn')) $('playSendBtn').disabled = true;
    if ($('playInput')) $('playInput').value = '';
    playState.messages.push({ role: 'user', content: text });
    var aiMsg = { role: 'assistant', content: '', streaming: true };
    playState.messages.push(aiMsg);
    renderPlayMessages();
    if ($('playStatus')) $('playStatus').textContent = '生成中…';
    if ($('playSuggest')) $('playSuggest').hidden = true;

    // prompts = 历史（去掉刚推入的 assistant 占位 + 当前 user）；content = 本轮
    var prompts = playState.messages.slice(0, -2).map(function (m) {
      return { role: m.role === 'user' ? 'user' : 'assistant', content: m.content || '' };
    });
    var deep = !!($('playDeepThinking') && $('playDeepThinking').checked);
    var mem = !!($('playMemoryEnhance') && $('playMemoryEnhance').checked);
    var body = {
      chatId: playState.chatId,
      cardId: playState.cardId,
      content: text,
      // 上下文长度体现在 model internalName；maxTokens = 最大回复 Token（设置里可改）
      model: playState.selectedModel || playState.defaultModel || '',
      maxTokens: playState.maxTokens || (deep ? 3500 : 2500),
      deepThinking: deep,
      enableMemoryEnhance: mem,
      style: playState.style || 'standard',
      imageGenerationModel: playState.imageGenerationModel || 'anime',
      presetIds: selectedPresetIds(),
      playerInfo: playState.playerInfo || '',
      prompts: prompts,
    };

    try {
      var res = await fetch('/api/card/play/send', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      if (!res.ok) {
        var errText = await res.text();
        var errMsg = errText;
        try {
          var ej = JSON.parse(errText);
          errMsg = ej.error || ej.message || errText;
        } catch (_) {}
        aiMsg.content = '（生成失败）' + errMsg;
        aiMsg.streaming = false;
        renderPlayMessages();
        showMsg(String(errMsg).slice(0, 200), false);
        return;
      }
      var reader = res.body && res.body.getReader ? res.body.getReader() : null;
      if (!reader) {
        var raw = await res.text();
        aiMsg.content = raw || '（空响应）';
        aiMsg.streaming = false;
        renderPlayMessages();
        return;
      }
      var dec = new TextDecoder();
      var buf = '';
      var content = '';
      while (true) {
        var step = await reader.read();
        if (step.done) break;
        buf += dec.decode(step.value, { stream: true });
        buf = parsePlaySseBuffer(buf, function (obj) {
          var typ = obj && obj.type;
          var data = obj && obj.data;
          if (typ === 'token') {
            var chunk = typeof data === 'string' ? data : '';
            content += chunk;
            aiMsg.content = content;
            renderPlayMessages();
          } else if (typ === 'step' && data && data.content != null) {
            content = String(data.content);
            aiMsg.content = content;
            renderPlayMessages();
          } else if (typ === 'complete') {
            if (data && data.content != null) content = String(data.content);
            else if (data && data.message != null) content = String(data.message);
            aiMsg.content = content || aiMsg.content;
            aiMsg.streaming = false;
            renderPlayMessages();
          } else if (typ === 'error') {
            var msg = (data && (data.message || data.error)) || '生成错误';
            aiMsg.content = (content ? content + '\n' : '') + '（错误）' + msg;
            aiMsg.streaming = false;
            renderPlayMessages();
            showMsg(String(msg).slice(0, 200), false);
          }
        });
      }
      // 尾部
      if (buf) {
        parsePlaySseBuffer(buf + '\n', function () {});
      }
      aiMsg.streaming = false;
      if (!aiMsg.content) aiMsg.content = '（无内容）';
      renderPlayMessages();
      if ($('playStatus')) $('playStatus').textContent = '就绪';
    } catch (e) {
      aiMsg.content = '（网络错误）' + String(e.message || e);
      aiMsg.streaming = false;
      renderPlayMessages();
      showMsg(String(e.message || e), false);
    } finally {
      playState.sending = false;
      if ($('playSendBtn')) $('playSendBtn').disabled = false;
    }
  }

  if ($('cardPlayBtn')) $('cardPlayBtn').addEventListener('click', enterCardPlay);
  if ($('cardPlayBackBtn')) {
    $('cardPlayBackBtn').addEventListener('click', function () {
      void exitCardPlay({ keepSession: false, keepPanel: false, deleteChat: true });
    });
  }
  if ($('playModelSelect')) {
    $('playModelSelect').addEventListener('change', onPlayModelGroupChange);
  }
  if ($('playRefreshMetaBtn')) {
    $('playRefreshMetaBtn').addEventListener('click', function () { loadPlayControls(); });
  }
  if ($('playPresetBtn')) {
    $('playPresetBtn').addEventListener('click', openPlayPresetModal);
  }
  if ($('playSettingsBtn')) {
    $('playSettingsBtn').addEventListener('click', openPlaySettingsModal);
  }
  if ($('playSettingsCloseBtn')) {
    $('playSettingsCloseBtn').addEventListener('click', closePlaySettingsModal);
  }
  if ($('playSettingsApplyBtn')) {
    $('playSettingsApplyBtn').addEventListener('click', function () { savePlaySettingsModal(); });
  }
  if ($('playSettingsModal')) {
    $('playSettingsModal').addEventListener('click', function (e) {
      if (e.target === $('playSettingsModal')) closePlaySettingsModal();
    });
  }
  if ($('playDeepThinking')) {
    $('playDeepThinking').addEventListener('change', function () {
      var on = !!$('playDeepThinking').checked;
      // 官网：切换深度思考时同步 maxTokens 3500/2500
      playState.maxTokens = on ? 3500 : 2500;
      if (playState.chatId) {
        patchPlaySettings({ deepThinking: on, maxTokens: playState.maxTokens }).catch(function () {});
      }
    });
  }
  if ($('playMemoryEnhance')) {
    $('playMemoryEnhance').addEventListener('change', function () {
      if (!playState.chatId) return;
      patchPlaySettings({
        enableMemoryEnhance: !!$('playMemoryEnhance').checked,
      }).catch(function () {});
    });
  }
  if ($('playPresetCloseBtn')) {
    $('playPresetCloseBtn').addEventListener('click', closePlayPresetModal);
  }
  if ($('playPresetApplyBtn')) {
    $('playPresetApplyBtn').addEventListener('click', applyPlayPresetModal);
  }
  if ($('playPresetClearBtn')) {
    $('playPresetClearBtn').addEventListener('click', function () {
      var list = $('playPresetList');
      if (!list) return;
      list.querySelectorAll('input[type="checkbox"]').forEach(function (cb) { cb.checked = false; });
    });
  }
  if ($('playPresetModal')) {
    $('playPresetModal').addEventListener('click', function (e) {
      if (e.target === $('playPresetModal')) closePlayPresetModal();
    });
  }
  if ($('playSendBtn')) $('playSendBtn').addEventListener('click', function () { sendPlayMessage(); });
  if ($('playInput')) {
    $('playInput').addEventListener('keydown', function (ev) {
      if (ev.key === 'Enter' && !ev.shiftKey && !ev.isComposing) {
        ev.preventDefault();
        sendPlayMessage();
      }
    });
  }
  bindTextZoomUi();
  updatePlayGate();
  if ($('cardRefreshBtn')) {
    $('cardRefreshBtn').addEventListener('click', function () { refreshCardList(); });
  }
  if ($('cardReloadBtn')) {
    $('cardReloadBtn').addEventListener('click', function () {
      if (!currentCardId) {
        showMsg('当前没有已保存的卡可重载', false);
        return;
      }
      openLocalCard(currentCardId);
    });
  }
  if ($('cardForm')) {
    // 网页改完某框失焦后，立刻吃本地磁盘更新（AI/编辑器写入）
    $('cardForm').addEventListener('focusout', function () {
      if (!currentCardId || !isCardMode() || cardPlayMode) return;
      setTimeout(function () { void pollCardOnce(); }, 30);
    });
  }
  if ($('cardCloudBtn')) {
    $('cardCloudBtn').addEventListener('click', async function () {
      setBusy(true);
      showMsg('正在拉取云端角色/草稿列表…', true);
      try {
        await refreshCloudCardList(true);
      } catch (e) {
        showMsg(String(e.message || e), false);
      } finally {
        setBusy(false);
      }
    });
  }
  function bindCloudFilter(id, mode) {
    var el = $(id);
    if (!el) return;
    el.addEventListener('click', function () {
      cloudCardFilter = mode;
      renderCloudCardList({ resetPage: true });
      var sc = $('cloudCardListScroll');
      if (sc) sc.scrollTop = 0;
    });
  }
  bindCloudFilter('cardCloudFilterAll', 'all');
  bindCloudFilter('cardCloudFilterDraft', 'draft');
  bindCloudFilter('cardCloudFilterPub', 'pub');
  if ($('cardCloudSearch')) {
    $('cardCloudSearch').addEventListener('input', function () {
      cloudCardSearch = $('cardCloudSearch').value || '';
      if (cloudSearchTimer) clearTimeout(cloudSearchTimer);
      cloudSearchTimer = setTimeout(function () {
        renderCloudCardList({ resetPage: true });
        var sc = $('cloudCardListScroll');
        if (sc) sc.scrollTop = 0;
      }, 120);
    });
  }

  /* —— 右侧官方 Workbench Agent —— */
  var agentState = {
    sessionId: '',
    backend: '',
    taskId: '',
    since: 0,
    polling: false,
    pollTimer: null,
    pollFails: 0,
    gameId: '',
    streamingEl: null,
    reasoningEl: null,
    toolOutputEl: null,
    streamedIds: {},
    seenToolKeys: {},
    hadAssistantText: false,
    hadToolActivity: false,
    autoContinueCount: 0,
    maxAutoContinue: 2,
    userStopped: false,
  };

  function resetAgentSession(reason) {
    agentState.sessionId = '';
    if (reason) setAgentHint(reason, true);
  }

  function maybeAutoContinue(reason) {
    if (agentState.userStopped) return false;
    if (agentState.polling) return false;
    if ((agentState.autoContinueCount || 0) >= (agentState.maxAutoContinue || 2)) return false;
    if (!agentState.sessionId) return false;
    agentState.autoContinueCount = (agentState.autoContinueCount || 0) + 1;
    var n = agentState.autoContinueCount;
    var max = agentState.maxAutoContinue || 2;
    appendAgentBubble('status', '检测到中断/无总结，自动发送「继续」（' + n + '/' + max + '）' + (reason ? ' · ' + reason : ''), {
      label: 'auto',
      collapse: false,
    });
    setAgentHint('自动续跑中…', true);
    setTimeout(function () {
      sendOfficialAgent({ text: '继续', retry: true, autoContinue: true });
    }, 450);
    return true;
  }

  function setAgentCollapsed(collapsed) {
    var app = $('appShell');
    if (!app) return;
    app.classList.toggle('is-agent-collapsed', !!collapsed);
    try { localStorage.setItem(AGENT_KEY, collapsed ? '1' : '0'); } catch (_) {}
  }

  function agentBackend() {
    return (($('agentBackend') && $('agentBackend').value) || 'claude').toLowerCase();
  }

  function setAgentHint(text, ok) {
    var el = $('agentHint');
    if (!el) return;
    el.textContent = text || '';
    el.classList.toggle('err', ok === false);
  }

  function clearAgentWelcome() {
    var box = $('agentMsgs');
    if (!box) return;
    var welcome = box.querySelector('.agent-welcome');
    if (welcome) welcome.remove();
  }

  var AGENT_FOLD_LINES = 8;
  var AGENT_FOLD_CHARS = 420;
  var AGENT_TOOL_OUT_MAX = 900;

  function agentShouldFold(role, text, opts) {
    opts = opts || {};
    if (opts.collapse === false) return false;
    if (opts.forceFold || opts.collapse === true) return true;
    if (role === 'tool' || role === 'reasoning') return true;
    if (opts.label === 'shell-out') return true;
    var t = text || '';
    // 单行超长 bash 也要折叠
    if (t.length > 120 && (role === 'tool' || /bash|shell|sed |rg |find |node /.test(t))) return true;
    if (t.split('\n').length > AGENT_FOLD_LINES) return true;
    if (t.length > AGENT_FOLD_CHARS) return true;
    if (/```/.test(t)) return true;
    return false;
  }

  function agentFoldSummary(role, text, opts) {
    opts = opts || {};
    var t = text || '';
    var lines = t ? t.split('\n').length : 0;
    if (role === 'tool' || opts.label === 'tool▶' || opts.label === 'tool') {
      var head = (opts.summary || t.split('\n')[0] || 'command').trim();
      if (head.length > 70) head = head.slice(0, 70) + '…';
      return (opts.label || 'tool') + ' · ' + head;
    }
    if (opts.label === 'shell-out' || role === 'tool_output') {
      return 'shell-out · ' + lines + ' 行 / ' + t.length + ' 字（点击展开）';
    }
    if (role === 'reasoning') {
      var tip = t.replace(/\s+/g, ' ').trim().slice(0, 48);
      return 'thinking' + (tip ? ' · ' + tip + (t.length > 48 ? '…' : '') : '');
    }
    return '长输出 · ' + lines + ' 行（点击展开）';
  }

  function updateAgentFoldSummary(ref) {
    if (!ref || !ref.summaryEl) return;
    var role = ref.role || 'assistant';
    var text = (ref.body && ref.body.textContent) || '';
    ref.summaryEl.textContent = agentFoldSummary(role, text, ref.opts || {});
  }

  function appendAgentBubble(role, text, opts) {
    opts = opts || {};
    clearAgentWelcome();
    var box = $('agentMsgs');
    if (!box) return null;
    var div = document.createElement('div');
    div.className = 'agent-bubble ' + (role || 'assistant');
    var label = document.createElement('span');
    label.className = 'agent-role';
    label.textContent = opts.label || role || 'assistant';
    div.appendChild(label);

    var fullText = text || '';
    if (opts.detail && opts.detail !== fullText) {
      fullText = fullText ? (fullText + '\n\n' + opts.detail) : opts.detail;
    }
    var fold = opts.collapse !== false && agentShouldFold(role, fullText, opts);
    var body;
    var summaryEl = null;
    var detailsEl = null;
    if (fold) {
      detailsEl = document.createElement('details');
      detailsEl.className = 'agent-fold';
      detailsEl.open = !!opts.open;
      summaryEl = document.createElement('summary');
      summaryEl.className = 'agent-fold-summary';
      summaryEl.textContent = agentFoldSummary(role, fullText, opts);
      body = document.createElement('pre');
      body.className = 'agent-body agent-fold-body';
      body.textContent = fullText;
      detailsEl.appendChild(summaryEl);
      detailsEl.appendChild(body);
      div.appendChild(detailsEl);
    } else {
      body = document.createElement('div');
      body.className = 'agent-body';
      body.textContent = fullText;
      div.appendChild(body);
    }
    box.appendChild(div);
    box.scrollTop = box.scrollHeight;
    return {
      root: div,
      body: body,
      detailsEl: detailsEl,
      summaryEl: summaryEl,
      role: role,
      opts: opts,
      truncated: false,
    };
  }

  function appendAgentTextDelta(text) {
    if (!text) return;
    agentState.reasoningEl = null;
    agentState.toolOutputEl = null;
    if (!agentState.streamingEl) {
      agentState.streamingEl = appendAgentBubble('assistant', '', { label: 'assistant', open: true });
    }
    if (!agentState.streamingEl) return;
    agentState.streamingEl.body.textContent += text;
    // 助手长文/代码块达到阈值后自动改成折叠（保持展开，不打断阅读）
    if (!agentState.streamingEl.detailsEl && agentShouldFold('assistant', agentState.streamingEl.body.textContent, {})) {
      var cur = agentState.streamingEl.body.textContent;
      var parent = agentState.streamingEl.root;
      var oldBody = agentState.streamingEl.body;
      var detailsEl = document.createElement('details');
      detailsEl.className = 'agent-fold';
      detailsEl.open = true;
      var summaryEl = document.createElement('summary');
      summaryEl.className = 'agent-fold-summary';
      var body = document.createElement('pre');
      body.className = 'agent-body agent-fold-body';
      body.textContent = cur;
      detailsEl.appendChild(summaryEl);
      detailsEl.appendChild(body);
      parent.replaceChild(detailsEl, oldBody);
      agentState.streamingEl.body = body;
      agentState.streamingEl.detailsEl = detailsEl;
      agentState.streamingEl.summaryEl = summaryEl;
      agentState.streamingEl.role = 'assistant';
    }
    updateAgentFoldSummary(agentState.streamingEl);
    var box = $('agentMsgs');
    if (box) box.scrollTop = box.scrollHeight;
  }

  function appendAgentReasoningDelta(text) {
    if (!text) return;
    agentState.streamingEl = null;
    agentState.toolOutputEl = null;
    if (!agentState.reasoningEl) {
      agentState.reasoningEl = appendAgentBubble('reasoning', '', { label: 'thinking', forceFold: true, open: false });
    }
    if (!agentState.reasoningEl) return;
    agentState.reasoningEl.body.textContent += text;
    updateAgentFoldSummary(agentState.reasoningEl);
    var box = $('agentMsgs');
    if (box) box.scrollTop = box.scrollHeight;
  }

  function appendAgentToolOutputDelta(text) {
    if (!text) return;
    agentState.streamingEl = null;
    agentState.reasoningEl = null;
    if (!agentState.toolOutputEl) {
      agentState.toolOutputEl = appendAgentBubble('tool', '', {
        label: 'shell-out',
        forceFold: true,
        open: false,
      });
    }
    if (!agentState.toolOutputEl) return;
    var body = agentState.toolOutputEl.body;
    var cur = body.textContent || '';
    if (agentState.toolOutputEl.truncated || cur.length >= AGENT_TOOL_OUT_MAX) {
      if (!agentState.toolOutputEl.truncated) {
        body.textContent = cur.slice(0, AGENT_TOOL_OUT_MAX) + '\n…(输出过长已截断，完整内容在容器内)';
        agentState.toolOutputEl.truncated = true;
        updateAgentFoldSummary(agentState.toolOutputEl);
      }
      return;
    }
    var next = cur + text;
    if (next.length > AGENT_TOOL_OUT_MAX) {
      body.textContent = next.slice(0, AGENT_TOOL_OUT_MAX) + '\n…(输出过长已截断，完整内容在容器内)';
      agentState.toolOutputEl.truncated = true;
    } else {
      body.textContent = next;
    }
    updateAgentFoldSummary(agentState.toolOutputEl);
    var box = $('agentMsgs');
    if (box) box.scrollTop = box.scrollHeight;
  }

  function stopAgentPoll() {
    agentState.polling = false;
    if (agentState.pollTimer) {
      clearTimeout(agentState.pollTimer);
      agentState.pollTimer = null;
    }
    if ($('agentCancelBtn')) $('agentCancelBtn').disabled = true;
    if ($('agentSendBtn')) $('agentSendBtn').disabled = false;
  }

  async function connectOfficialAgent() {
    setAgentHint('正在连接官方容器…', true);
    try {
      var data = await api('/api/agent/ready', { method: 'POST', body: '{}' });
      if (data.status) fillForm(data.status);
      if (!data.ok) {
        setAgentHint(data.error || '连接失败', false);
        if ($('agentMeta')) $('agentMeta').textContent = '未连接';
        return false;
      }
      agentState.gameId = data.gameId || '';
      if ($('agentMeta')) {
        $('agentMeta').textContent =
          'gameId=' + (data.gameId || '—') +
          (data.editorStatus ? ' · ' + data.editorStatus : '') +
          (data.ttlSeconds ? ' · ttl ' + data.ttlSeconds + 's' : '');
      }
      setAgentHint('已连接官方 Agent（' + agentBackend() + '）', true);
      return true;
    } catch (e) {
      setAgentHint(String(e.message || e), false);
      return false;
    }
  }

  function agentElapsedHint(extra) {
    var started = agentState.pollStartedAt || Date.now();
    var sec = Math.max(0, Math.round((Date.now() - started) / 1000));
    var base = '官方助手执行中… ' + sec + 's';
    return extra ? (base + ' · ' + extra) : base;
  }

  async function pollOfficialAgent() {
    if (!agentState.polling || !agentState.taskId) return;
    try {
      var data = await api('/api/agent/poll', {
        method: 'POST',
        body: JSON.stringify({
          taskId: agentState.taskId,
          backend: agentBackend(),
          since: agentState.since || 0,
          gameId: agentState.gameId || '',
        }),
      });
      // 轮询时不要 fillForm→renderPreview，否则预览/布局会被反复挤动
      if (!data.ok) {
        agentState.pollFails = (agentState.pollFails || 0) + 1;
        if (agentState.pollFails < 20) {
          setAgentHint(agentElapsedHint('轮询重试 ' + agentState.pollFails), true);
          agentState.pollTimer = setTimeout(pollOfficialAgent, Math.min(2000, 600 + agentState.pollFails * 100));
          return;
        }
        setAgentHint(data.error || '轮询失败', false);
        stopAgentPoll();
        return;
      }
      agentState.pollFails = 0;
      agentState.since = data.since || agentState.since;
      if (data.gameId) agentState.gameId = data.gameId;
      // 只用事件里的 conversation session 续聊
      if (data.sessionId) {
        agentState.sessionId = data.sessionId;
        if ($('agentMeta')) {
          $('agentMeta').textContent =
            'gameId=' + (agentState.gameId || '—') +
            ' · session ' + String(agentState.sessionId).slice(0, 8) + '…';
        }
      }
      var deltas = data.deltas || [];
      var sessionLost = false;
      for (var i = 0; i < deltas.length; i++) {
        var d = deltas[i];
        if (!d) continue;
        var itemId = d.itemId ? String(d.itemId) : '';
        if (d.kind === 'text') {
          if (itemId && d.stream) agentState.streamedIds[itemId] = true;
          // 已流式输出过的整段 full 消息跳过，避免只在结束时再刷一整坨
          if (d.full && itemId && agentState.streamedIds[itemId] && !d.replace) continue;
          if (d.full && !d.stream && agentState.streamingEl && !d.replace) {
            // 无 itemId 的完整消息：若已有流式气泡则跳过
            if ((agentState.streamingEl.body.textContent || '').length > 0) continue;
          }
          if (d.replace) {
            agentState.streamingEl = null;
            agentState.reasoningEl = null;
            agentState.toolOutputEl = null;
            appendAgentBubble('assistant', d.text || '', { label: 'assistant', open: true });
          } else {
            appendAgentTextDelta(d.text || '');
          }
          if (d.text) agentState.hadAssistantText = true;
        } else if (d.kind === 'tool') {
          var toolKey = (itemId || '') + '|' + (d.tool || '') + '|' + String(d.text || '').slice(0, 100);
          if (toolKey && agentState.seenToolKeys[toolKey]) continue;
          if (toolKey) agentState.seenToolKeys[toolKey] = true;
          agentState.hadToolActivity = true;
          agentState.streamingEl = null;
          agentState.reasoningEl = null;
          agentState.toolOutputEl = null;
          var toolBody = d.text || d.tool || 'tool';
          if (d.tool && d.text && d.text !== d.tool) toolBody = d.tool + ': ' + d.text;
          else if (d.tool && !d.text) toolBody = d.tool;
          appendAgentBubble(toolBody.indexOf('\n') >= 0 || (d.detail) ? 'tool' : 'tool', toolBody, {
            label: d.status === 'running' ? 'tool▶' : 'tool',
            detail: d.detail || '',
            summary: (d.tool || 'tool') + (d.text && d.text !== d.tool ? (': ' + String(d.text).split('\n')[0]) : ''),
            forceFold: true,
            open: false,
          });
          setAgentHint(agentElapsedHint('调用 ' + (d.tool || 'tool')), true);
        } else if (d.kind === 'tool_output') {
          agentState.hadToolActivity = true;
          var outText = d.text || '';
          var outLines = outText ? outText.split('\n').length : 0;
          if (d.detail || outLines > 6 || outText.length > 240) {
            agentState.streamingEl = null;
            agentState.reasoningEl = null;
            agentState.toolOutputEl = null;
            appendAgentBubble('tool', outText, {
              label: 'shell-out',
              detail: d.detail || '',
              forceFold: true,
              open: false,
            });
          } else {
            appendAgentToolOutputDelta(outText);
          }
          setAgentHint(agentElapsedHint('工具返回'), true);
        } else if (d.kind === 'reasoning') {
          if (d.stream) appendAgentReasoningDelta(d.text || '');
          else {
            agentState.streamingEl = null;
            agentState.reasoningEl = null;
            agentState.toolOutputEl = null;
            appendAgentBubble('reasoning', d.text || '', { label: 'thinking', forceFold: true, open: false });
          }
        } else if (d.kind === 'status') {
          agentState.streamingEl = null;
          agentState.reasoningEl = null;
          agentState.toolOutputEl = null;
          var stText = d.text || '';
          if (/No conversation found|session ID/i.test(stText)) {
            sessionLost = true;
            appendAgentBubble('status', '会话已失效，已清空；请再发一条（会开新对话）', { label: 'error' });
          } else if (String(d.status || '') === 'info') {
            appendAgentBubble('status', stText, { label: 'info' });
          } else {
            appendAgentBubble('status', stText, { label: d.status || 'status' });
          }
        }
      }
      if (sessionLost) resetAgentSession();
      if (data.done) {
        var taskStatus = data.taskStatus || '';
        if (typeof taskStatus !== 'string') {
          try { taskStatus = JSON.stringify(taskStatus); } catch (_) { taskStatus = String(taskStatus); }
        }
        agentState.streamingEl = null;
        agentState.reasoningEl = null;
        agentState.toolOutputEl = null;
        var okDone = !sessionLost && taskStatus === 'completed';
        var failed = /^(failed|error|cancelled)$/i.test(taskStatus);
        var needContinue =
          !sessionLost &&
          !agentState.userStopped &&
          (
            (okDone && !agentState.hadAssistantText && agentState.hadToolActivity) ||
            failed
          );
        stopAgentPoll();
        if (needContinue && maybeAutoContinue(okDone ? '无文字总结' : ('状态 ' + taskStatus))) {
          return;
        }
        if (okDone && !agentState.hadAssistantText) {
          appendAgentBubble('status', '本轮结束，但未收到文字总结。可手动发「继续」或「给出结论摘要」。', { label: 'info' });
        }
        setAgentHint(
          sessionLost
            ? '会话失效，已重置，请重新发送'
            : (okDone ? '本轮完成' : ('任务结束：' + (taskStatus || 'done'))),
          okDone
        );
        return;
      }
      if (!(deltas && deltas.length)) {
        setAgentHint(agentElapsedHint('等待模型/工具…'), true);
      } else if (!data.done) {
        setAgentHint(agentElapsedHint(), true);
      }
    } catch (e) {
      // 长任务期间偶发断连：不要停死，继续轮询
      agentState.pollFails = (agentState.pollFails || 0) + 1;
      if (agentState.polling && agentState.pollFails < 20) {
        setAgentHint(agentElapsedHint('网络抖动 ' + agentState.pollFails), true);
        agentState.pollTimer = setTimeout(pollOfficialAgent, Math.min(2500, 800 + agentState.pollFails * 120));
        return;
      }
      setAgentHint(String(e.message || e), false);
      stopAgentPoll();
      // 轮询彻底失败时自动续跑
      if (!agentState.userStopped && agentState.sessionId) {
        maybeAutoContinue('轮询中断');
      }
      return;
    }
    agentState.pollTimer = setTimeout(pollOfficialAgent, 700);
  }

  async function sendOfficialAgent(opts) {
    opts = opts || {};
    var text = opts.text || (($('agentInput') && $('agentInput').value.trim()) || '');
    if (!text) {
      setAgentHint('请输入内容', false);
      return;
    }
    if (agentState.polling) {
      setAgentHint('上一轮仍在执行，可先点停止', false);
      return;
    }
    var be = agentBackend();
    if (agentState.backend && agentState.backend !== be) {
      resetAgentSession('已切换后端，新开对话');
    }
    agentState.backend = be;
    agentState.userStopped = false;
    if (!opts.retry && !opts.autoContinue) {
      appendAgentBubble('user', text, { label: 'you' });
      if ($('agentInput')) $('agentInput').value = '';
      agentState.autoContinueCount = 0;
    } else if (opts.autoContinue) {
      var autoBubble = appendAgentBubble('user', text, { label: 'auto·继续', collapse: false });
      if (autoBubble && autoBubble.root) autoBubble.root.classList.add('is-auto');
    }
    agentState.streamingEl = null;
    agentState.reasoningEl = null;
    agentState.toolOutputEl = null;
    agentState.streamedIds = {};
    agentState.seenToolKeys = {};
    agentState.hadAssistantText = false;
    agentState.hadToolActivity = false;
    if ($('agentSendBtn')) $('agentSendBtn').disabled = true;
    setAgentHint(
      opts.autoContinue
        ? '自动续跑：继续…'
        : (opts.retry ? '会话失效，正在新开对话重试…' : '提交中…'),
      true
    );
    try {
      var data = await api('/api/agent/send', {
        method: 'POST',
        body: JSON.stringify({
          prompt: text,
          backend: be,
          // 仅传事件里拿到的 conversation session；空则新开
          sessionId: opts.forceNew ? '' : (agentState.sessionId || ''),
        }),
      });
      if (data.status) fillForm(data.status, { skipPreview: true });
      if (!data.ok) {
        var err = data.error || '发送失败';
        if (!opts.retry && /No conversation found|session ID/i.test(err)) {
          resetAgentSession();
          return sendOfficialAgent({ text: text, retry: true, forceNew: true });
        }
        setAgentHint(err, false);
        if ($('agentSendBtn')) $('agentSendBtn').disabled = false;
        return;
      }
      agentState.taskId = data.taskId || '';
      // 发送响应里的 sessionId 不可靠；等 poll 事件回填
      agentState.gameId = data.gameId || agentState.gameId;
      agentState.since = 0;
      agentState.polling = true;
      agentState.pollFails = 0;
      agentState.pollStartedAt = Date.now();
      if ($('agentCancelBtn')) $('agentCancelBtn').disabled = false;
      if ($('agentMeta')) {
        $('agentMeta').textContent =
          'gameId=' + (agentState.gameId || '—') +
          (agentState.sessionId ? ' · session ' + String(agentState.sessionId).slice(0, 8) + '…' : ' · 等待会话…');
      }
      setAgentHint('官方助手执行中…', true);
      pollOfficialAgent();
    } catch (e) {
      setAgentHint(String(e.message || e), false);
      if ($('agentSendBtn')) $('agentSendBtn').disabled = false;
    }
  }

  if ($('agentCollapseBtn')) {
    $('agentCollapseBtn').addEventListener('click', function () {
      setAgentCollapsed(true);
    });
  }
  if ($('agentToggleBtn')) {
    $('agentToggleBtn').addEventListener('click', function () {
      var app = $('appShell');
      var collapsed = !!(app && app.classList.contains('is-agent-collapsed'));
      setAgentCollapsed(!collapsed);
      if (!collapsed) return;
      // 展开时若尚未连接，尝试一次
      if (!agentState.gameId) connectOfficialAgent();
    });
  }
  if ($('agentReadyBtn')) {
    $('agentReadyBtn').addEventListener('click', function () {
      connectOfficialAgent();
    });
  }
  if ($('agentSendBtn')) {
    $('agentSendBtn').addEventListener('click', function () {
      sendOfficialAgent();
    });
  }
  if ($('agentCancelBtn')) {
    $('agentCancelBtn').addEventListener('click', async function () {
      var tid = agentState.taskId;
      agentState.userStopped = true; // 手动停止后不再自动「继续」
      stopAgentPoll();
      agentState.streamingEl = null;
      if (!tid) {
        setAgentHint('已停止等待', true);
        return;
      }
      try {
        await api('/api/agent/cancel', {
          method: 'POST',
          body: JSON.stringify({ taskId: tid, backend: agentBackend() }),
        });
      } catch (_) {}
      setAgentHint('已请求停止（不会自动续跑）', true);
    });
  }
  if ($('agentInput')) {
    $('agentInput').addEventListener('keydown', function (ev) {
      if (ev.key === 'Enter' && !ev.shiftKey && !ev.isComposing) {
        ev.preventDefault();
        sendOfficialAgent();
      }
    });
  }
  if ($('agentBackend')) {
    $('agentBackend').addEventListener('change', function () {
      agentState.backend = agentBackend();
      resetAgentSession('已切换 ' + agentState.backend + '，下一轮将新开会话');
    });
  }

  try {
    if (localStorage.getItem(SIDEBAR_KEY) === '1') setSidebarCollapsed(true);
    else setSidebarCollapsed(false);
  } catch (_) {
    setSidebarCollapsed(false);
  }
  try {
    // 默认展开右侧官方助手；仅当用户主动收起过才保持收起
    if (localStorage.getItem(AGENT_KEY) === '1') setAgentCollapsed(true);
    else setAgentCollapsed(false);
  } catch (_) {
    setAgentCollapsed(false);
  }
  setFabProgress(0, 'idle');

  // 刷新后内存态丢失：强制退出试玩叠层，避免空表单+试玩条并存
  setCardPlayMode(false);
  try {
    var savedMode = localStorage.getItem(CONSOLE_MODE_KEY);
    var savedCardId = '';
    try { savedCardId = localStorage.getItem(CONSOLE_CARD_KEY) || ''; } catch (_) {}
    if (savedMode === 'card' || savedMode === 'game') {
      switchConsoleMode(savedMode, { forcePersist: true });
    } else {
      // 首次使用：默认角色卡（写卡只需登录态；游戏预览不自动拉起）
      switchConsoleMode('card', { forcePersist: true });
    }
    // 角色卡模式：恢复上次打开的卡夹（刷新后不再是空白错误页）
    if ((savedMode === 'card' || consoleMode === 'card') && savedCardId) {
      openLocalCard(savedCardId).catch(function () {});
    }
  } catch (_) {
    switchConsoleMode('card', { forcePersist: true });
  }

  document.addEventListener('keydown', function (ev) {
    if (ev.key === 'Escape' && isSidebarCollapsed()) {
      setSidebarCollapsed(false);
    }
  });

  refresh().then(function (status) {
    if (consoleMode === 'card') {
      detachGameRuntime();
      return;
    }
    if (status && status.pull && status.pull.running) {
      setBusy(true);
      startPullPoll();
      switchConsoleMode('game', { skipPanel: true });
      switchPanel('pull');
    }
    if (status && status.sync && status.sync.running) {
      setBusy(true, { keepEnabled: { fabSync: true, fabExpand: true, fabReload: true } });
      applySyncJob(status.sync);
      startSyncPoll();
    }
    if (status && status.preview && status.preview.running) {
      setStageLive(true);
      startBridgePoll();
    } else {
      renderBridge(null, status && status.preview);
    }
  }).catch(function (e) {
    showMsg(String(e.message || e), false);
  });
}());
