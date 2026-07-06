    const state = {
      accountList: [],
      adapterList: [],
      dashboard: null,
      backupStatus: null,
      backupRunResult: null,
      backupSettingsSaving: false,
      captureTargetList: [],
      capturePolicyError: '',
      adminTokenList: [],
      adminTokenCreateResult: null,
      adminTokenError: '',
      adminUserList: [],
      adminUserCreateResult: null,
      adminUserError: '',
      adminSessionList: [],
      adminSessionError: '',
      authIdentity: null,
      authError: '',
      currentRobot: null,
      roomList: [],
      currentRoom: null,
      messageList: [],
      selectionMode: false,
      selectedMessageHashes: new Set(),
      longPressTimer: 0,
      longPressPointerId: null,
      selectionImageUrl: '',
      searchResults: [],
      searchMode: false,
      loadingRooms: false,
      loadingHistory: false,
      highlightedMessageId: '',
      replyJumpMissingId: '',
      settingsMode: false,
      editingAdapterId: null,
      adapterEditorOpen: false,
      importValidationReport: null,
      offlineAuditReport: null,
      offlineRepairReport: null,
      uiLogs: [],
      theme: localStorage.getItem('chatAuditTheme') || 'light',
      themeTransitionTimer: 0,
      themeTransitionFrame: 0,
      restoringRoute: false,
      palette: JSON.parse(localStorage.getItem('chatAuditPalette') || 'null') || {
        primary: '#1d4ed8',
        secondary: '#dbeafe',
        contrast: '#14b8a6',
      },
      adminApiToken: localStorage.getItem('chatAuditAdminApiToken') || '',
    };

    const el = (id) => document.getElementById(id);
    const text = (value) => value === null || value === undefined ? '' : String(value);
    const currentRole = () => state.authIdentity ? state.authIdentity.role : (state.adminApiToken ? 'admin' : 'viewer');
    const roleRank = (role) => ({ viewer: 1, operator: 2, admin: 3 })[role] || 1;
    const canRole = (role) => roleRank(currentRole()) >= roleRank(role);

    const promptForAdminApiToken = () => {
      const token = window.prompt('请输入管理 API Token');
      if (token && token.trim()) {
        state.adminApiToken = token.trim();
        localStorage.setItem('chatAuditAdminApiToken', state.adminApiToken);
        return true;
      }
      return false;
    };

    const authHeaders = (headers = {}) => {
      if (!state.adminApiToken) return headers;
      return { ...headers, Authorization: `Bearer ${state.adminApiToken}` };
    };

    const cookieValue = (name) => {
      const prefix = `${name}=`;
      return document.cookie
        .split(';')
        .map((part) => part.trim())
        .find((part) => part.startsWith(prefix))
        ?.slice(prefix.length) || '';
    };

    const csrfHeaders = (method, headers = {}) => {
      if (method === 'GET') return headers;
      const token = cookieValue('chat_audit_csrf');
      return token ? { ...headers, 'X-CSRF-Token': token } : headers;
    };

    const responseErrorMessage = async (response, url) => {
      let detail = '';
      try {
        const payload = await response.clone().json();
        detail = payload && (payload.detail || payload.message || payload.error || JSON.stringify(payload));
      } catch {
        try {
          detail = await response.text();
        } catch {
          detail = '';
        }
      }
      return detail ? `HTTP ${response.status}: ${detail}` : `HTTP ${response.status}: ${url}`;
    };

    const sleep = (ms) => new Promise((resolve) => window.setTimeout(resolve, ms));

    const friendlyHttpErrorMessage = (response, technicalMessage) => {
      if (response.status === 401) return '登录已失效，请重新输入 Token';
      if (response.status === 403) return '权限不足，请检查 Token 角色';
      if (response.status === 413) return '请求内容过大，请缩小范围或文件后重试';
      if (response.status === 429) {
        const retryAfter = response.headers.get('Retry-After') || '60';
        return `操作太频繁，请 ${retryAfter} 秒后重试`;
      }
      if (response.status >= 500) return '服务器暂时不可用，请稍后重试';
      return technicalMessage;
    };

    const friendlyNetworkErrorMessage = (error) => {
      if (error && error.name === 'AbortError') return '请求超时，请稍后重试';
      return '网络连接失败，请检查服务是否在线';
    };

    const shouldRetryResponse = (response) => response.status === 429 || response.status >= 500;

    const requestWithRetry = async (url, options = {}, { retries = 2 } = {}) => {
      const method = options.method || 'GET';
      for (let attempt = 0; attempt <= retries; attempt += 1) {
        try {
          const headers = csrfHeaders(method, authHeaders(options.headers || {}));
          let response = await fetch(url, { ...options, headers });
          if (response.status === 401 && attempt === 0 && promptForAdminApiToken()) {
            response = await fetch(url, { ...options, headers: csrfHeaders(method, authHeaders(options.headers || {})) });
          }
          if (response.ok) {
            pushUiLog(`${method} ${url} · ${response.status}`);
            return response;
          }
          const technicalMessage = await responseErrorMessage(response, url);
          const userMessage = friendlyHttpErrorMessage(response, technicalMessage);
          if (shouldRetryResponse(response) && attempt < retries) {
            pushUiLog(`${method} ${url} retry ${attempt + 1}/${retries}: ${userMessage}`, 'warning');
            await sleep(1000 * (attempt + 1));
            continue;
          }
          const error = new Error(userMessage);
          error.technicalMessage = technicalMessage;
          throw error;
        } catch (error) {
          if (error.technicalMessage) throw error;
          if (attempt < retries) {
            const userMessage = friendlyNetworkErrorMessage(error);
            pushUiLog(`${method} ${url} retry ${attempt + 1}/${retries}: ${userMessage}`, 'warning');
            await sleep(1000 * (attempt + 1));
            continue;
          }
          throw new Error(friendlyNetworkErrorMessage(error));
        }
      }
      throw new Error('请求失败，请稍后重试');
    };

    const requestJson = async (url, options = {}) => {
      const method = options.method || 'GET';
      const withAuth = {
        ...options,
        headers: csrfHeaders(method, authHeaders(options.headers || {})),
      };
      const response = await requestWithRetry(url, options);
      if (response.status === 401 && promptForAdminApiToken()) {
        response = await fetch(url, { ...options, headers: csrfHeaders(method, authHeaders(options.headers || {})) });
      }
      if (!response.ok) {
        const message = await responseErrorMessage(response, url);
        pushUiLog(`${method} ${url} 失败：${message}`, 'error');
        throw new Error(message);
      }
      pushUiLog(`${method} ${url} · ${response.status}`);
      if (response.status === 204) return null;
      return await response.json();
    };

    const requestBlob = async (url, options = {}) => {
      const method = options.method || 'GET';
      const withAuth = {
        ...options,
        headers: csrfHeaders(method, authHeaders(options.headers || {})),
      };
      const response = await requestWithRetry(url, options);
      if (response.status === 401 && promptForAdminApiToken()) {
        response = await fetch(url, { ...options, headers: csrfHeaders(method, authHeaders(options.headers || {})) });
      }
      if (!response.ok) {
        const message = await responseErrorMessage(response, url);
        pushUiLog(`${method} ${url} failed: ${message}`, 'error');
        throw new Error(message);
      }
      pushUiLog(`${method} ${url} · ${response.status}`);
      return await response.blob();
    };

    const apiGet = (url) => requestJson(url);
    const apiSend = (url, method, body = null) => requestJson(url, {
      method,
      headers: body !== null ? { 'Content-Type': 'application/json' } : {},
      body: body !== null ? JSON.stringify(body) : null,
    });

    const statusDotClass = (status) => `status-dot status-${['green', 'red', 'gray'].includes(status) ? status : 'gray'}`;
    const formatTs = (ts) => ts ? new Date(ts * 1000).toLocaleString() : '-';
    const avatarText = (id) => {
      const value = text(id).trim();
      return value ? value.slice(-2).toUpperCase() : '--';
    };
    const renderAvatar = (container, id, type = 'user', src = '') => {
      clearNode(container);
      container.textContent = avatarText(id);
      const url = normalizeSafeMediaSrc(src);
      if (!url) return;
      const img = document.createElement('img');
      img.alt = '';
      img.loading = 'lazy';
      img.decoding = 'async';
      img.referrerPolicy = 'no-referrer';
      img.addEventListener('error', () => img.remove(), { once: true });
      img.src = url;
      container.appendChild(img);
    };
    const isMedia = (value) => typeof value === 'string' && value.includes('/static/storage/');
    const isImg = (value) => /\.(png|jpe?g|gif|webp|bmp|svg)$/i.test(value);
    const isVideo = (value) => /\.(mp4|webm|mov|mkv)$/i.test(value);
    const isVoice = (value) => /\.(mp3|wav|ogg|silk|amr|m4a)$/i.test(value);
    const isLocalPath = (value) => typeof value === 'string' && value.startsWith('/static/storage/');
    const isRemoteUrl = (value) => /^https?:\/\//i.test(String(value || ''));
    const isSafeProtocol = (url) => ['http:', 'https:'].includes(url.protocol);
    const SAFE_IDENTIFIER_PATTERN = /^[A-Za-z0-9_.:@-]+$/;
    const SAFE_USERNAME_PATTERN = /^[A-Za-z0-9_.@-]+$/;
    const SAFE_ROLES = new Set(['viewer', 'operator', 'admin']);
    const SAFE_ADAPTER_STATUSES = new Set(['green', 'red', 'gray']);
    const SAFE_CAPTURE_LIST_MODES = new Set(['none', 'blacklist', 'whitelist']);
    const MAX_ADAPTER_CONFIG_BYTES = 64 * 1024;
    const MAX_IMPORT_PACKAGE_BYTES = 5 * 1024 * 1024;
    const normalizeSafeUrl = (value, { allowLocal = false } = {}) => {
      const raw = String(value || '').trim();
      if (!raw) return '';
      if (allowLocal && isLocalPath(raw)) return raw;
      if (raw.startsWith('/')) return '';
      const normalized = raw.startsWith('//')
        ? `https:${raw}`
        : (/^[a-z][a-z0-9+.-]*:/i.test(raw) ? raw : `https://${raw}`);
      try {
        const parsed = new URL(normalized, window.location.origin);
        return isSafeProtocol(parsed) ? parsed.href : '';
      } catch {
        return '';
      }
    };
    const normalizeSafeMediaSrc = (value) => normalizeSafeUrl(value, { allowLocal: true });
    const fileName = (value) => text(value).split('/').pop() || 'media';
    const URL_TEXT_PATTERN = /((?:https?:\/\/|www\.)[^\s<>"']+)/ig;
    const TRAILING_URL_PUNCTUATION = /[),.;:!?，。；：！？、）】》]+$/;
    const decodeHtmlEntities = (value) => {
      const textarea = document.createElement('textarea');
      textarea.innerHTML = value;
      return textarea.value;
    };

    const normalizeTextUrl = (url) => {
      return normalizeSafeUrl(url);
    };

    const utf8ByteLength = (value) => new Blob([String(value || '')]).size;

    const validationError = (message) => {
      pushUiLog(message, 'warning');
      return null;
    };

    const normalizedIdentifier = (value, label, { required = true, max = 64, pattern = SAFE_IDENTIFIER_PATTERN } = {}) => {
      const normalized = text(value).trim();
      if (!normalized) return required ? validationError(`${label} is required`) : '';
      if (normalized.length > max) return validationError(`${label} is too long`);
      if (!pattern.test(normalized)) return validationError(`${label} contains unsupported characters`);
      return normalized;
    };

    const normalizedChoice = (value, allowed, label) => {
      const normalized = text(value).trim();
      if (!allowed.has(normalized)) return validationError(`${label} is invalid`);
      return normalized;
    };

    const normalizedInteger = (value, label, { min = 0, max = Number.MAX_SAFE_INTEGER, required = true } = {}) => {
      const normalized = text(value).trim();
      if (!normalized) return required ? validationError(`${label} is required`) : null;
      if (!/^\d+$/.test(normalized)) return validationError(`${label} must be a non-negative integer`);
      const parsed = Number(normalized);
      if (!Number.isSafeInteger(parsed) || parsed < min || parsed > max) return validationError(`${label} is out of range`);
      return parsed;
    };

    const normalizedTimestamp = (value, label) => {
      const normalized = text(value).trim();
      if (!normalized) return '';
      const parsed = normalizedInteger(normalized, label, { min: 0, max: 9999999999 });
      return parsed === null ? null : String(parsed);
    };

    const normalizeCronValue = (value) => {
      const normalized = text(value).trim();
      if (!normalized || ['off', 'disabled', 'none', 'false', '0'].includes(normalized.toLowerCase())) return normalized || 'off';
      const parts = normalized.split(/\s+/);
      if (parts.length !== 5) return validationError('backup cron must be off or a 5-field cron');
      if (!parts.every((part) => /^[0-9*/,\-]+$/.test(part) && part.length <= 20)) {
        return validationError('backup cron contains unsupported characters');
      }
      return parts.join(' ');
    };

    const parseJsonObjectInput = (value, label, { maxBytes = MAX_ADAPTER_CONFIG_BYTES, required = false } = {}) => {
      const raw = text(value).trim();
      if (!raw) return required ? validationError(`${label} is required`) : {};
      if (utf8ByteLength(raw) > maxBytes) return validationError(`${label} is too large`);
      try {
        const parsed = JSON.parse(raw);
        if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return validationError(`${label} must be a JSON object`);
        return parsed;
      } catch {
        return validationError(`${label} is not valid JSON`);
      }
    };

    const routeParams = () => new URLSearchParams(window.location.hash.replace(/^#/, ''));

    const routeValue = (key) => {
      const value = routeParams().get(key);
      return value && SAFE_IDENTIFIER_PATTERN.test(value) ? value : '';
    };

    const writeRouteState = ({ replace = false } = {}) => {
      if (state.restoringRoute) return;
      const params = new URLSearchParams();
      if (state.currentRobot) params.set('robot', state.currentRobot.id);
      if (state.currentRoom) params.set('room', state.currentRoom.room_id);
      const nextHash = params.toString() ? `#${params.toString()}` : '';
      const nextUrl = `${window.location.pathname}${window.location.search}${nextHash}`;
      const currentUrl = `${window.location.pathname}${window.location.search}${window.location.hash}`;
      if (nextUrl === currentUrl) return;
      if (replace) window.history.replaceState(null, '', nextUrl);
      else window.history.pushState(null, '', nextUrl);
    };

    const restoreRouteState = async () => {
      const robotId = routeValue('robot');
      if (!robotId || !state.accountList.length) return false;
      const account = state.accountList.find((item) => item.id === robotId);
      if (!account) return false;
      const roomId = routeValue('room');
      state.restoringRoute = true;
      try {
        if (!state.currentRobot || state.currentRobot.id !== account.id) {
          await switchAccount(account, { updateRoute: false });
        }
        if (roomId) {
          const room = state.roomList.find((item) => item.room_id === roomId);
          if (room && (!state.currentRoom || state.currentRoom.room_id !== room.room_id)) {
            await selectRoom(room, { updateRoute: false });
          }
        }
        return true;
      } finally {
        state.restoringRoute = false;
        writeRouteState({ replace: true });
      }
    };

    const appendLinkedText = (container, value, className = 'cq-text') => {
      const source = String(value || '');
      if (!source) return null;
      const wrapper = document.createElement('span');
      wrapper.className = className;
      let cursor = 0;
      let matched = false;
      let match = null;
      URL_TEXT_PATTERN.lastIndex = 0;
      while ((match = URL_TEXT_PATTERN.exec(source)) !== null) {
        matched = true;
        const rawMatch = match[0];
        const trailing = rawMatch.match(TRAILING_URL_PUNCTUATION)?.[0] || '';
        const clean = trailing ? rawMatch.slice(0, -trailing.length) : rawMatch;
        wrapper.appendChild(document.createTextNode(source.slice(cursor, match.index)));
        if (clean) {
          const href = normalizeTextUrl(clean);
          if (!href) {
            wrapper.appendChild(document.createTextNode(clean));
            if (trailing) wrapper.appendChild(document.createTextNode(trailing));
            cursor = match.index + rawMatch.length;
            continue;
          }
          const link = document.createElement('a');
          link.className = 'message-link';
          link.href = href;
          link.target = '_blank';
          link.rel = 'noreferrer';
          link.textContent = clean;
          wrapper.appendChild(link);
        }
        if (trailing) wrapper.appendChild(document.createTextNode(trailing));
        cursor = match.index + rawMatch.length;
      }
      wrapper.appendChild(document.createTextNode(source.slice(cursor)));
      if (!matched) wrapper.textContent = source;
      container.appendChild(wrapper);
      return wrapper;
    };

    const clearNode = (node) => {
      while (node.firstChild) node.removeChild(node.firstChild);
    };

    const button = (label, className, onClick) => {
      const node = document.createElement('button');
      node.className = className;
      node.type = 'button';
      node.textContent = label;
      node.addEventListener('click', onClick);
      return node;
    };

    const pushUiLog = (message, level = 'info') => {
      state.uiLogs.unshift({
        level,
        message,
        time: new Date().toLocaleString(),
      });
      state.uiLogs = state.uiLogs.slice(0, 80);
      renderActivityLogs();
    };

    const applyPalette = () => {
      const root = document.documentElement;
      root.style.setProperty('--primary', state.palette.primary);
      root.style.setProperty('--secondary', state.palette.secondary);
      root.style.setProperty('--contrast', state.palette.contrast);
      const primaryInput = el('primaryColorInput');
      const secondaryInput = el('secondaryColorInput');
      const contrastInput = el('contrastColorInput');
      if (primaryInput) primaryInput.value = state.palette.primary;
      if (secondaryInput) secondaryInput.value = state.palette.secondary;
      if (contrastInput) contrastInput.value = state.palette.contrast;
    };

    const savePalette = () => {
      localStorage.setItem('chatAuditPalette', JSON.stringify(state.palette));
      applyPalette();
      pushUiLog(`调色盘已更新：${state.palette.primary} / ${state.palette.secondary} / ${state.palette.contrast}`);
    };

    const setThemeMode = (mode) => {
      const nextTheme = mode === 'dark' ? 'dark' : 'light';
      if (state.theme === nextTheme) return;
      document.body.classList.add('theme-switching');
      document.body.getBoundingClientRect();
      if (state.themeTransitionFrame) {
        window.cancelAnimationFrame(state.themeTransitionFrame);
      }
      window.clearTimeout(state.themeTransitionTimer);
      state.themeTransitionTimer = window.setTimeout(() => {
        document.body.classList.remove('theme-switching');
      }, 680);
      state.themeTransitionFrame = window.requestAnimationFrame(() => {
        state.theme = nextTheme;
        localStorage.setItem('chatAuditTheme', state.theme);
        applyTheme();
        state.themeTransitionFrame = 0;
      });
      pushUiLog(`主题已切换为${nextTheme === 'dark' ? '夜间' : '白天'}`);
    };

    const applyTheme = () => {
      document.body.dataset.theme = state.theme;
      const day = el('dayModeButton');
      const night = el('nightModeButton');
      if (day) day.classList.toggle('active', state.theme !== 'dark');
      if (night) night.classList.toggle('active', state.theme === 'dark');
    };

    const toggleTheme = () => {
      setThemeMode(state.theme === 'dark' ? 'light' : 'dark');
    };

    const renderActivityLogs = () => {
      const container = el('activityLogList');
      if (!container) return;
      clearNode(container);
      if (!state.uiLogs.length) {
        container.appendChild(Object.assign(document.createElement('div'), { className: 'hint', textContent: '暂无操作日志' }));
        const summary = el('topLogSummary');
        if (summary) summary.textContent = '暂无记录';
        return;
      }
      const summary = el('topLogSummary');
      if (summary) summary.textContent = state.uiLogs[0].message;
      state.uiLogs.forEach((entry) => {
        const item = document.createElement('div');
        item.className = 'log-item';
        const title = document.createElement('strong');
        title.textContent = entry.message;
        const meta = document.createElement('span');
        meta.textContent = `${entry.time} · ${entry.level}`;
        item.append(title, meta);
        container.appendChild(item);
      });
    };

    const renderTopbarAccount = () => {
      const avatar = el('topAccountAvatar');
      if (!avatar) return;
      renderAvatar(avatar, state.currentRobot ? state.currentRobot.id : '', 'bot', state.currentRobot ? state.currentRobot.avatar_path : '');
      const config = el('topbarConfigText');
      if (!config) return;
      const account = state.currentRobot ? `${state.currentRobot.display_name || state.currentRobot.id} · ${state.currentRobot.platform}` : '未选择账号';
      const room = state.currentRoom ? roomDisplayName(state.currentRoom) : '未选择会话';
      config.textContent = `${account} / ${room}`;
    };

    const renderAccounts = () => {
      const container = el('accountList');
      clearNode(container);
      state.accountList.forEach((acc) => {
        const node = document.createElement('button');
        node.className = `avatar-button${state.currentRobot && state.currentRobot.id === acc.id ? ' active' : ''}`;
        node.title = `${acc.display_name || acc.id} · ${acc.platform} · ${acc.status}`;
        node.type = 'button';
        node.addEventListener('click', () => switchAccount(acc));

        const avatar = document.createElement('div');
        avatar.className = 'avatar';
        renderAvatar(avatar, acc.id, 'bot', acc.avatar_path);
        node.appendChild(avatar);

        const dot = document.createElement('span');
        dot.className = statusDotClass(acc.status);
        node.appendChild(dot);
        container.appendChild(node);
      });
      el('accountCount').textContent = state.accountList.length;
      renderTopbarAccount();
    };

    const adapterStatusText = (status) => {
      if (status === 'green') return '已启用';
      if (status === 'red') return '异常';
      return '未启用';
    };

    const adapterDisplayName = (adapter) => adapter.current_robot_id || adapter.id || '未命名适配器';

    const renderAdapters = () => {
      const container = el('adapterList');
      clearNode(container);
      if (state.adapterList.length === 0) {
        container.appendChild(Object.assign(document.createElement('div'), {
          className: 'adapter-empty',
          textContent: '还没有适配器，点击右上角新增适配器开始接入。',
        }));
        return;
      }
      state.adapterList.forEach((acc) => {
        const card = document.createElement('article');
        card.className = 'adapter-robot-card';

        const main = document.createElement('div');
        const title = document.createElement('h3');
        title.textContent = adapterDisplayName(acc);
        const meta = document.createElement('div');
        meta.className = 'adapter-card-meta';
        const binding = acc.current_robot_id ? `绑定身份：${acc.current_robot_id}` : '绑定身份：等待适配器上报';
        [binding, `平台：${acc.platform}`, `适配器 ID：${acc.id}`, `状态：${adapterStatusText(acc.status)}（${acc.status}）`].forEach((line) => {
          const node = document.createElement('span');
          node.className = 'truncate';
          node.textContent = line;
          meta.appendChild(node);
        });

        const actions = document.createElement('div');
        actions.className = 'adapter-card-actions';
        const remove = button('删除', 'adapter-pill delete', () => deleteAdapter(acc.id));
        const edit = button('编辑', 'adapter-pill edit', () => editAdapter(acc));
        remove.disabled = !canRole('admin');
        edit.disabled = !canRole('operator');
        actions.append(remove, edit);
        main.append(title, meta, actions);

        const side = document.createElement('div');
        side.className = 'adapter-card-side';
        const switchLabel = document.createElement('label');
        switchLabel.className = 'switch-control';
        switchLabel.title = '启用 / 停用适配器';
        const enabled = document.createElement('input');
        enabled.type = 'checkbox';
        enabled.checked = acc.status === 'green';
        enabled.disabled = !canRole('operator');
        enabled.addEventListener('change', () => toggleAdapterEnabled(acc, enabled.checked));
        const track = document.createElement('span');
        track.className = 'switch-track';
        switchLabel.append(enabled, track);

        const logo = document.createElement('div');
        logo.className = 'adapter-logo-mark';
        logo.textContent = avatarText(acc.current_robot_id || acc.id);
        side.append(switchLabel, logo);
        card.append(main, side);
        container.appendChild(card);
      });
    };

    const renderDashboard = () => {
      const container = el('dashboardSummary');
      clearNode(container);
      const dashboard = state.dashboard || {};
      [
        ['messages', dashboard.messages || 0],
        ['rooms', dashboard.rooms || 0],
        ['media', dashboard.media_assets || 0],
        ['backups', dashboard.backups || 0],
      ].forEach(([label, value]) => {
        const item = document.createElement('div');
        item.className = 'dashboard-stat';
        const number = document.createElement('strong');
        number.textContent = value;
        const caption = document.createElement('span');
        caption.textContent = label;
        item.append(number, caption);
        container.appendChild(item);
      });
    };

    const renderRooms = () => {
      const container = el('roomList');
      clearNode(container);
      if (state.loadingRooms) {
        container.appendChild(Object.assign(document.createElement('div'), { className: 'loading', textContent: '正在加载会话...' }));
        return;
      }
      if (state.roomList.length === 0) {
        container.appendChild(Object.assign(document.createElement('div'), { className: 'empty', textContent: '暂无可见会话' }));
        return;
      }
      state.roomList.forEach((room) => {
        const row = document.createElement('button');
        row.className = `room-item${state.currentRoom && state.currentRoom.room_id === room.room_id ? ' active' : ''}`;
        row.type = 'button';
        row.addEventListener('click', () => selectRoom(room));

        const thumb = document.createElement('div');
        thumb.className = 'room-thumb';
        const avatar = document.createElement('div');
        avatar.className = 'room-avatar';
        renderAvatar(avatar, room.room_id, 'group', room.avatar_path);
        thumb.appendChild(avatar);

        const body = document.createElement('div');
        body.style.minWidth = '0';
        body.style.flex = '1';
        const name = document.createElement('div');
        name.className = 'truncate';
        name.textContent = roomDisplayName(room);
        const meta = document.createElement('div');
        meta.className = 'hint truncate';
        meta.textContent = `最近存盘: ${formatTs(room.last_timestamp)}`;
        body.append(name, meta);
        row.append(thumb, body);
        container.appendChild(row);
      });
    };

    const renderSearchResults = () => {
      const panel = el('searchPanel');
      panel.classList.toggle('open', state.searchMode);
      el('searchResultCount').textContent = state.searchResults.length;
      const container = el('searchResults');
      clearNode(container);
      if (!state.searchMode) return;
      if (state.searchResults.length === 0) {
        container.appendChild(Object.assign(document.createElement('div'), { className: 'empty', textContent: '没有匹配消息' }));
        return;
      }
      state.searchResults.forEach((result) => {
        const item = document.createElement('button');
        item.type = 'button';
        item.className = 'result-item';
        item.addEventListener('click', () => selectSearchResult(result));
        const meta = document.createElement('div');
        meta.className = 'result-meta';
        meta.innerHTML = `<span></span><span></span>`;
        meta.children[0].textContent = `${result.room_id} · ${result.nickname || result.sender_id}`;
        meta.children[1].textContent = formatTs(result.timestamp);
        const body = document.createElement('div');
        body.className = 'truncate';
        body.textContent = result.local_message;
        item.append(meta, body);
        container.appendChild(item);
      });
    };

    const plainMessagePreview = (value) => {
      const raw = text(value)
        .replace(/\[CQ:reply,[^\]]+\]/g, '')
        .replace(/\[CQ:at,qq=([^\],]+)[^\]]*\]/g, '@$1')
        .replace(/\[CQ:image,[^\]]+\]/g, '[图片]')
        .replace(/\[CQ:record,[^\]]+\]/g, '[语音]')
        .replace(/\[CQ:video,[^\]]+\]/g, '[视频]')
        .replace(/\[CQ:forward,[^\]]+\]/g, '[合并转发]')
        .replace(/\[CQ:json,[^\]]+\]/g, '[卡片]')
        .replace(/\[CQ:poke[^\]]*\]/g, '[戳一戳]')
        .replace(/\/static\/storage\/[a-f0-9]{32}\.[a-z0-9]+/ig, '[媒体]');
      const compact = raw.replace(/\s+/g, ' ').trim();
      return compact || '[消息]';
    };

    const findReplyMessage = (replyId) => {
      if (!replyId) return null;
      return state.messageList.find((msg) => String(msg.external_message_id || '') === String(replyId)) || null;
    };

    const replyPreviewText = (replyId) => {
      if (!replyId) return '';
      const owner = state.messageList.find((msg) => String(msg.reply_to_message_id || '') === String(replyId) && msg.reply_preview_text);
      return owner ? owner.reply_preview_text : '';
    };

    const roomDisplayName = (room) => {
      if (!room) return '请选择会话';
      return room.display_name ? `${room.display_name}（${room.room_id}）` : room.room_id;
    };

    const renderSettings = () => {
      el('settingsPanel').classList.toggle('open', state.settingsMode);
      el('settingsToggle').classList.toggle('active', state.settingsMode);
      const adapterEditorOpen = Boolean(state.adapterEditorOpen || state.editingAdapterId);
      el('adapterEditorBackdrop').hidden = !adapterEditorOpen;
      el('adapterEditor').hidden = !adapterEditorOpen;
      el('adapterId').disabled = Boolean(state.editingAdapterId);
      el('adapterEnabledSwitch').checked = el('adapterStatus').value === 'green';
      el('adapterEditorTitle').textContent = state.editingAdapterId
        ? `编辑 ${el('adapterId').value || state.editingAdapterId} 适配器`
        : '新增适配器';
      el('saveAdapterButton').textContent = state.editingAdapterId ? '保存修改' : '添加适配器';
      renderAuthStatus();
      renderCapturePolicies();
      renderBackupStatus();
      renderAdminTokens();
      renderAdminUsers();
      renderAdminSessions();
      renderAdapters();
      applyRoleUi();
    };

    const mountAdapterEditorPortal = () => {
      ['adapterEditorBackdrop', 'adapterEditor'].forEach((id) => {
        const node = el(id);
        if (node.parentElement !== document.body) {
          document.body.appendChild(node);
        }
      });
    };

    const resetAdapterEditorScroll = () => {
      requestAnimationFrame(() => {
        const editor = el('adapterEditor');
        if (!editor.hidden) editor.scrollTop = 0;
      });
    };

    const renderAuthStatus = () => {
      const identity = state.authIdentity;
      el('authActor').textContent = identity ? `${identity.actor} · ${identity.role}` : (state.adminApiToken ? 'Token 已设置' : '未登录');
      el('logoutButton').disabled = !state.adminApiToken;
      const box = el('authStatusReport');
      clearNode(box);
      const rows = [
        ['actor', identity ? identity.actor : '-'],
        ['role', identity ? identity.role : '-'],
        ['username', identity && identity.username ? identity.username : '-'],
      ];
      if (state.authError) rows.push(['error', state.authError, 'bad']);
      rows.forEach(([name, value, className]) => {
        const row = document.createElement('div');
        const label = document.createElement('strong');
        label.textContent = `${name}: `;
        const valueNode = document.createElement('span');
        if (className) valueNode.className = className;
        valueNode.textContent = value;
        row.append(label, valueNode);
        box.appendChild(row);
      });
    };

    const capturePolicyValue = (target, key) => {
      const policy = target.policy || {};
      if (key === 'list_mode') return policy.list_mode || 'none';
      if (Object.prototype.hasOwnProperty.call(policy, key)) return Boolean(policy[key]);
      return key === 'capture_file' ? false : true;
    };

    const targetDisplayName = (target) => {
      const name = target.display_name || target.target_id;
      return target.display_name ? `${name} (${target.target_id})` : target.target_id;
    };

    const renderCapturePolicies = () => {
      const container = el('capturePolicyList');
      clearNode(container);
      if (!state.currentRobot) {
        container.appendChild(Object.assign(document.createElement('div'), { className: 'hint', textContent: '先选择一个机器人角色档案' }));
        return;
      }
      if (state.capturePolicyError) {
        container.appendChild(Object.assign(document.createElement('div'), { className: 'bad', textContent: state.capturePolicyError }));
        return;
      }
      if (!state.captureTargetList.length) {
        container.appendChild(Object.assign(document.createElement('div'), { className: 'hint', textContent: '暂无已发现群聊或私聊；收到消息后会自动出现在这里' }));
        return;
      }
      state.captureTargetList.forEach((target) => {
        const row = document.createElement('div');
        row.className = 'capture-policy-item';
        const head = document.createElement('div');
        head.className = 'capture-policy-head';
        const title = document.createElement('strong');
        title.className = 'truncate';
        title.textContent = `${target.target_type === 'group' ? '群聊' : '私聊'} · ${targetDisplayName(target)}`;
        const meta = document.createElement('span');
        meta.className = 'hint';
        meta.textContent = target.policy ? '已设置' : '默认';
        head.append(title, meta);

        const grid = document.createElement('div');
        grid.className = 'capture-policy-grid';
        const mode = document.createElement('select');
        mode.dataset.field = 'list_mode';
        [
          ['none', '默认'],
          ['blacklist', '黑名单'],
          ['whitelist', '白名单'],
        ].forEach(([value, label]) => {
          const option = document.createElement('option');
          option.value = value;
          option.textContent = label;
          mode.appendChild(option);
        });
        mode.value = capturePolicyValue(target, 'list_mode');
        grid.appendChild(mode);

        [
          ['capture_text', '文字'],
          ['capture_image', '图片'],
          ['capture_voice', '语音'],
          ['capture_video', '视频'],
          ['capture_file', '文件包/文档'],
        ].forEach(([field, label]) => {
          const wrap = document.createElement('label');
          wrap.className = 'capture-check';
          const input = document.createElement('input');
          input.type = 'checkbox';
          input.dataset.field = field;
          input.checked = capturePolicyValue(target, field);
          wrap.append(input, document.createTextNode(label));
          grid.appendChild(wrap);
        });

        const actions = document.createElement('div');
        actions.className = 'button-row';
        const save = button('保存', 'btn primary', () => saveCapturePolicy(target, row));
        const reset = button('恢复默认', 'btn', () => resetCapturePolicy(target));
        save.disabled = !canRole('operator');
        reset.disabled = !target.policy || !canRole('operator');
        actions.append(save, reset);
        grid.appendChild(actions);

        row.append(head, grid);
        container.appendChild(row);
      });
    };

    const backupPresetInput = () => document.querySelector('input[name="backupPreset"]:checked');

    const pad2 = (value) => String(value).padStart(2, '0');

    const parseBackupCron = (cron) => {
      const raw = text(cron).trim();
      if (!raw || raw.toLowerCase() === 'off') {
        return { preset: 'off', time: '03:00', weekday: '1', cron: raw || 'off' };
      }
      const parts = raw.split(/\s+/);
      if (parts.length === 5 && /^\d+$/.test(parts[0]) && /^\d+$/.test(parts[1]) && parts[2] === '*' && parts[3] === '*') {
        const hour = Math.max(0, Math.min(23, Number(parts[1])));
        const minute = Math.max(0, Math.min(59, Number(parts[0])));
        if (parts[4] === '*') {
          return { preset: 'daily', time: `${pad2(hour)}:${pad2(minute)}`, weekday: '1', cron: raw };
        }
        if (/^[0-6]$/.test(parts[4])) {
          return { preset: 'weekly', time: `${pad2(hour)}:${pad2(minute)}`, weekday: parts[4], cron: raw };
        }
      }
      return { preset: 'custom', time: '03:00', weekday: '1', cron: raw };
    };

    const composeBackupCron = () => {
      const preset = backupPresetInput() ? backupPresetInput().value : 'off';
      const [hourRaw, minuteRaw] = (el('backupTimeInput').value || '03:00').split(':');
      const hour = Number(hourRaw || 3);
      const minute = Number(minuteRaw || 0);
      const safeHour = Math.max(0, Math.min(23, Number.isFinite(hour) ? hour : 3));
      const safeMinute = Math.max(0, Math.min(59, Number.isFinite(minute) ? minute : 0));
      if (preset === 'daily') return `${safeMinute} ${safeHour} * * *`;
      if (preset === 'weekly') return `${safeMinute} ${safeHour} * * ${el('backupWeekdayInput').value || '1'}`;
      if (preset === 'custom') return el('backupCronInput').value.trim();
      return 'off';
    };

    const applyBackupPresetVisibility = () => {
      const preset = backupPresetInput() ? backupPresetInput().value : 'off';
      const fixedMode = preset === 'daily' || preset === 'weekly';
      el('backupTimeInput').disabled = !fixedMode;
      el('backupWeekdayInput').disabled = preset !== 'weekly';
      el('backupCronCustomField').hidden = preset !== 'custom';
      if (preset !== 'custom') el('backupCronInput').value = composeBackupCron();
    };

    const setBackupUiFromCron = (cron) => {
      const parsed = parseBackupCron(cron);
      const radio = document.querySelector(`input[name="backupPreset"][value="${parsed.preset}"]`);
      if (radio) radio.checked = true;
      el('backupTimeInput').value = parsed.time;
      el('backupWeekdayInput').value = parsed.weekday;
      el('backupCronInput').value = parsed.cron;
      applyBackupPresetVisibility();
    };

    const syncBackupCronFromControls = () => {
      el('backupCronInput').value = composeBackupCron();
      applyBackupPresetVisibility();
    };

    const renderBackupStatus = () => {
      const box = el('backupStatusReport');
      clearNode(box);
      const status = state.backupStatus;
      if (!status) {
        box.textContent = '备份状态未加载';
        return;
      }
      setBackupUiFromCron(status.cron || '');
      el('backupKeepLatestInput').value = status.keep_latest === null || status.keep_latest === undefined ? '' : status.keep_latest;
      el('saveBackupSettingsButton').disabled = state.backupSettingsSaving;
      el('resetBackupSettingsButton').disabled = state.backupSettingsSaving;
      const rows = [
        ['状态', status.enabled ? '开启' : '关闭', status.enabled ? 'ok' : undefined],
        ['计划', status.cron || '-'],
        ['保留', status.keep_latest],
        ['配置来源', status.config_source || '-'],
        ['计划来源', status.cron_source || '-'],
        ['保留来源', status.keep_latest_source || '-'],
        ['备份数量', status.backups],
        ['最近备份', status.latest_backup || '-'],
      ];
      if (state.backupRunResult) {
        rows.push(['手动备份', state.backupRunResult.filename || '-', 'ok']);
      }
      rows.forEach(([name, value, className]) => {
        const row = document.createElement('div');
        const label = document.createElement('strong');
        label.textContent = `${name}：`;
        const valueNode = document.createElement('span');
        if (className) valueNode.className = className;
        valueNode.textContent = value;
        row.append(label, valueNode);
        box.appendChild(row);
      });
    };

    const renderAdminTokens = () => {
      const report = el('adminTokenCreateReport');
      clearNode(report);
      report.hidden = !state.adminTokenCreateResult && !state.adminTokenError;
      if (state.adminTokenCreateResult) {
        const title = document.createElement('strong');
        title.textContent = '新令牌：';
        const token = document.createElement('span');
        token.textContent = state.adminTokenCreateResult.token;
        report.append(title, token);
      } else if (state.adminTokenError) {
        const error = document.createElement('span');
        error.className = 'bad';
        error.textContent = state.adminTokenError;
        report.appendChild(error);
      }

      const container = el('adminTokenList');
      clearNode(container);
      if (!state.adminTokenList.length) {
        const empty = document.createElement('div');
        empty.className = 'hint';
        empty.textContent = '暂无数据库托管令牌';
        container.appendChild(empty);
        return;
      }
      state.adminTokenList.forEach((token) => {
        const row = document.createElement('div');
        row.className = 'adapter-item';
        const label = document.createElement('span');
        label.className = 'truncate';
        label.textContent = `${token.name} · ${token.role} · ${token.status} · ${token.token_prefix}`;
        const revoke = button('吊销', 'btn link danger', () => revokeAdminToken(token.id));
        revoke.disabled = token.status === 'revoked' || !canRole('admin');
        row.append(label, revoke);
        container.appendChild(row);
      });
    };

    const renderAdminUsers = () => {
      const report = el('adminUserCreateReport');
      clearNode(report);
      report.hidden = !state.adminUserCreateResult && !state.adminUserError;
      if (state.adminUserCreateResult) {
        const ok = document.createElement('span');
        ok.className = 'ok';
        ok.textContent = `${state.adminUserCreateResult.username} · ${state.adminUserCreateResult.role}`;
        report.appendChild(ok);
      } else if (state.adminUserError) {
        const error = document.createElement('span');
        error.className = 'bad';
        error.textContent = state.adminUserError;
        report.appendChild(error);
      }

      const container = el('adminUserList');
      clearNode(container);
      if (!state.adminUserList.length) {
        const empty = document.createElement('div');
        empty.className = 'hint';
        empty.textContent = '暂无数据库用户';
        container.appendChild(empty);
        return;
      }
      state.adminUserList.forEach((user) => {
        const row = document.createElement('div');
        row.className = 'adapter-item';
        const label = document.createElement('span');
        label.className = 'truncate';
        label.textContent = `${user.username} · ${user.role} · ${user.status}`;
        const reset = button('重置密码', 'btn link', () => resetAdminUserPassword(user.id));
        const revoke = button('禁用', 'btn link danger', () => revokeAdminUser(user.id));
        reset.disabled = user.status === 'revoked' || !canRole('admin');
        revoke.disabled = user.status === 'revoked' || !canRole('admin');
        row.append(label, reset, revoke);
        container.appendChild(row);
      });
    };

    const renderAdminSessions = () => {
      const container = el('adminSessionList');
      clearNode(container);
      if (state.adminSessionError) {
        const error = document.createElement('div');
        error.className = 'bad';
        error.textContent = state.adminSessionError;
        container.appendChild(error);
        return;
      }
      if (!state.adminSessionList.length) {
        const empty = document.createElement('div');
        empty.className = 'hint';
        empty.textContent = '暂无登录会话';
        container.appendChild(empty);
        return;
      }
      state.adminSessionList.forEach((session) => {
        const row = document.createElement('div');
        row.className = 'adapter-item';
        const label = document.createElement('span');
        label.className = 'truncate';
        label.textContent = `${session.username} · ${session.role} · ${session.status} · ${session.token_prefix}`;
        const revoke = button('强制下线', 'btn link danger', () => revokeAdminSession(session.id));
        revoke.disabled = session.status === 'revoked' || !canRole('admin');
        row.append(label, revoke);
        container.appendChild(row);
      });
    };

    const applyRoleUi = () => {
      const isAdmin = canRole('admin');
      const isOperator = canRole('operator');
      el('createAdminTokenButton').disabled = !isAdmin;
      el('createAdminUserButton').disabled = !isAdmin;
      el('refreshAdminSessionsButton').disabled = !isAdmin;
      el('refreshCapturePoliciesButton').disabled = !state.currentRobot;
      el('saveAdapterButton').disabled = !isOperator;
      el('runBackupButton').disabled = !isOperator;
      el('saveBackupSettingsButton').disabled = el('saveBackupSettingsButton').disabled || !isOperator;
      el('resetBackupSettingsButton').disabled = el('resetBackupSettingsButton').disabled || !isOperator;
      el('repairOfflineAuditButton').disabled = !isOperator;
      el('openImportButton').disabled = !isAdmin;
    };

    const messageKey = (msg) => String(
      msg.msg_hash
      || msg.external_message_id
      || `${msg.timestamp || 0}:${msg.sender_id || ''}:${msg.local_message || msg.raw_message || ''}`
    );

    const selectedMessages = () => state.messageList.filter((msg) => state.selectedMessageHashes.has(messageKey(msg)));

    const renderSelectionToolbar = () => {
      const toolbar = el('selectionToolbar');
      if (!toolbar) return;
      const count = state.selectedMessageHashes.size;
      toolbar.classList.toggle('open', state.selectionMode);
      el('selectionCount').textContent = count ? `已选 ${count} 条` : '长按消息进入多选';
      el('exportSelectedButton').disabled = count === 0;
      el('renderSelectedImageButton').disabled = count === 0;
      el('selectAllVisibleButton').disabled = !state.messageList.length;
    };

    const enterSelectionMode = (msgHash) => {
      if (!msgHash) return;
      state.selectionMode = true;
      state.selectedMessageHashes.add(msgHash);
      pushUiLog('进入消息多选模式');
      renderChat();
    };

    const exitSelectionMode = () => {
      window.clearTimeout(state.longPressTimer);
      state.longPressTimer = 0;
      state.longPressPointerId = null;
      state.selectionMode = false;
      state.selectedMessageHashes.clear();
      renderChat();
    };

    const toggleMessageSelection = (msgHash) => {
      if (!msgHash) return;
      if (state.selectedMessageHashes.has(msgHash)) {
        state.selectedMessageHashes.delete(msgHash);
      } else {
        state.selectedMessageHashes.add(msgHash);
      }
      if (state.selectionMode && state.selectedMessageHashes.size === 0) {
        state.selectionMode = false;
      }
      renderChat();
    };

    const isSelectionInteractiveTarget = (target) => Boolean(target.closest && target.closest('button, a, audio, video, input, select, textarea'));

    const bindMessageSelectionEvents = (row, msg) => {
      const msgHash = messageKey(msg);
      row.addEventListener('click', (event) => {
        if (!state.selectionMode) return;
        event.preventDefault();
        event.stopPropagation();
        toggleMessageSelection(msgHash);
      });
      row.addEventListener('pointerdown', (event) => {
        if (event.button !== 0 || state.selectionMode || isSelectionInteractiveTarget(event.target)) return;
        state.longPressPointerId = event.pointerId;
        window.clearTimeout(state.longPressTimer);
        state.longPressTimer = window.setTimeout(() => {
          state.longPressTimer = 0;
          enterSelectionMode(msgHash);
        }, 450);
      });
      const cancelLongPress = () => {
        window.clearTimeout(state.longPressTimer);
        state.longPressTimer = 0;
        state.longPressPointerId = null;
      };
      row.addEventListener('pointerup', cancelLongPress);
      row.addEventListener('pointercancel', cancelLongPress);
      row.addEventListener('pointerleave', cancelLongPress);
    };

    const selectAllVisibleMessages = () => {
      state.selectionMode = true;
      state.messageList.forEach((msg) => {
        state.selectedMessageHashes.add(messageKey(msg));
      });
      renderChat();
    };

    const downloadBlob = (blob, filename) => {
      const url = URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = url;
      link.download = filename;
      document.body.appendChild(link);
      link.click();
      link.remove();
      window.setTimeout(() => URL.revokeObjectURL(url), 1000);
    };

    const selectedExportPayload = () => ({
      schema: 'chat-audit-selected-messages/v1',
      exported_at: new Date().toISOString(),
      robot: state.currentRobot,
      room: state.currentRoom,
      messages: selectedMessages(),
    });

    const exportSelectedMessages = () => {
      const payload = selectedExportPayload();
      if (!payload.messages.length) return;
      const blob = new Blob([JSON.stringify(payload, null, 2)], { type: 'application/json;charset=utf-8' });
      downloadBlob(blob, `chat-audit-selected-${Date.now()}.json`);
      pushUiLog(`导出选定记录：${payload.messages.length} 条`);
    };

    const wrapCanvasText = (ctx, value, maxWidth) => {
      const rawLines = text(value).split(/\n/);
      const lines = [];
      rawLines.forEach((rawLine) => {
        let line = '';
        Array.from(rawLine || ' ').forEach((char) => {
          const next = line + char;
          if (ctx.measureText(next).width > maxWidth && line) {
            lines.push(line);
            line = char;
          } else {
            line = next;
          }
        });
        lines.push(line);
      });
      return lines;
    };

    const selectedMessageImageRows = (messages) => {
      const canvas = document.createElement('canvas');
      const ctx = canvas.getContext('2d');
      ctx.font = '15px Microsoft YaHei, Arial, sans-serif';
      const maxTextWidth = 760;
      return messages.map((msg) => {
        const preview = plainMessagePreview(msg.local_message || msg.raw_message);
        const lines = wrapCanvasText(ctx, preview, maxTextWidth);
        return { msg, lines, height: 56 + lines.length * 22 };
      });
    };

    const roundedRectPath = (ctx, x, y, width, height, radius) => {
      if (ctx.roundRect) {
        ctx.roundRect(x, y, width, height, radius);
        return;
      }
      const r = Math.min(radius, width / 2, height / 2);
      ctx.moveTo(x + r, y);
      ctx.lineTo(x + width - r, y);
      ctx.quadraticCurveTo(x + width, y, x + width, y + r);
      ctx.lineTo(x + width, y + height - r);
      ctx.quadraticCurveTo(x + width, y + height, x + width - r, y + height);
      ctx.lineTo(x + r, y + height);
      ctx.quadraticCurveTo(x, y + height, x, y + height - r);
      ctx.lineTo(x, y + r);
      ctx.quadraticCurveTo(x, y, x + r, y);
    };

    const renderSelectedMessagesImage = () => {
      const messages = selectedMessages();
      if (!messages.length) return;
      const rows = selectedMessageImageRows(messages);
      const width = 900;
      const height = Math.max(220, 86 + rows.reduce((sum, row) => sum + row.height + 14, 0));
      const scale = Math.min(window.devicePixelRatio || 1, 2);
      const canvas = document.createElement('canvas');
      canvas.width = width * scale;
      canvas.height = height * scale;
      canvas.style.width = `${width}px`;
      canvas.style.height = `${height}px`;
      const ctx = canvas.getContext('2d');
      ctx.scale(scale, scale);
      ctx.fillStyle = '#eef6ff';
      ctx.fillRect(0, 0, width, height);
      ctx.fillStyle = '#0f172a';
      ctx.font = '700 24px Microsoft YaHei, Arial, sans-serif';
      ctx.fillText(roomDisplayName(state.currentRoom) || '选定聊天记录', 32, 42);
      ctx.font = '13px Microsoft YaHei, Arial, sans-serif';
      ctx.fillStyle = '#64748b';
      ctx.fillText(`${state.currentRobot ? state.currentRobot.display_name || state.currentRobot.id : ''} · ${messages.length} 条 · ${new Date().toLocaleString()}`, 32, 66);
      let y = 92;
      rows.forEach(({ msg, lines, height: rowHeight }) => {
        ctx.fillStyle = '#ffffff';
        ctx.strokeStyle = '#dbeafe';
        ctx.lineWidth = 1;
        const x = 32;
        const w = width - 64;
        const h = rowHeight;
        ctx.beginPath();
        roundedRectPath(ctx, x, y, w, h, 14);
        ctx.fill();
        ctx.stroke();
        ctx.fillStyle = '#0f766e';
        ctx.beginPath();
        ctx.arc(x + 28, y + 28, 18, 0, Math.PI * 2);
        ctx.fill();
        ctx.fillStyle = '#ffffff';
        ctx.font = '700 11px Microsoft YaHei, Arial, sans-serif';
        ctx.textAlign = 'center';
        ctx.fillText(text(msg.nickname || msg.sender_id).slice(-2).toUpperCase(), x + 28, y + 32);
        ctx.textAlign = 'left';
        ctx.fillStyle = '#475569';
        ctx.font = '13px Microsoft YaHei, Arial, sans-serif';
        ctx.fillText(`${msg.nickname || msg.sender_id} · ${formatTs(msg.timestamp)}`, x + 58, y + 24);
        ctx.fillStyle = '#111827';
        ctx.font = '15px Microsoft YaHei, Arial, sans-serif';
        lines.slice(0, 12).forEach((line, index) => {
          ctx.fillText(line, x + 58, y + 50 + index * 22);
        });
        y += h + 14;
      });
      canvas.toBlob((blob) => {
        if (!blob) return;
        const filename = `chat-audit-selected-${Date.now()}.png`;
        downloadBlob(blob, filename);
        if (state.selectionImageUrl) URL.revokeObjectURL(state.selectionImageUrl);
        state.selectionImageUrl = URL.createObjectURL(blob);
        openImagePreview(state.selectionImageUrl, filename);
        pushUiLog(`渲染选定记录图片：${messages.length} 条`);
      }, 'image/png');
    };

    const renderChat = () => {
      el('roomTitle').textContent = roomDisplayName(state.currentRoom);
      el('robotView').textContent = state.currentRobot ? ` 视角: ${state.currentRobot.display_name || state.currentRobot.id}` : '';
      el('refreshButton').disabled = !state.currentRoom;
      el('openExportButton').disabled = !state.currentRobot;
      renderTopbarAccount();
      el('chatEmpty').hidden = Boolean(state.currentRoom);
      el('chatWindow').hidden = !state.currentRoom;
      const chat = el('chatWindow');
      clearNode(chat);
      if (!state.currentRoom) {
        renderSelectionToolbar();
        return;
      }
      if (state.loadingHistory) {
        const loading = document.createElement('div');
        loading.className = 'hint';
        loading.style.textAlign = 'center';
        loading.textContent = '正在加载更早记录...';
        chat.appendChild(loading);
      }
      if (state.messageList.length === 0) {
        const empty = document.createElement('div');
        empty.className = 'empty';
        empty.style.textAlign = 'center';
        empty.textContent = '暂无消息';
        chat.appendChild(empty);
        renderSelectionToolbar();
        return;
      }
      state.messageList.forEach((msg) => {
        const mine = state.currentRobot && msg.sender_id === state.currentRobot.id;
        const msgKey = messageKey(msg);
        const selected = state.selectedMessageHashes.has(msgKey);
        const row = document.createElement('div');
        row.className = `message${mine ? ' mine' : ''}${state.selectionMode ? ' selection-active' : ''}${selected ? ' selected' : ''}`;
        if (msg.external_message_id) row.dataset.externalMessageId = String(msg.external_message_id);
        row.dataset.msgHash = msgKey;
        const selector = document.createElement('div');
        selector.className = 'message-select-dot';
        selector.textContent = selected ? '✓' : '';
        selector.setAttribute('aria-hidden', 'true');
        if (state.highlightedMessageId && String(msg.external_message_id || '') === String(state.highlightedMessageId)) {
          row.className += ' reply-jump-highlight';
        }
        const avatar = document.createElement('div');
        avatar.className = 'avatar message-avatar';
        renderAvatar(avatar, msg.sender_id, 'user', msg.sender_avatar_path);
        const body = document.createElement('div');
        body.className = 'message-body';
        const meta = document.createElement('div');
        meta.className = 'message-meta';
        meta.textContent = `${msg.nickname || msg.sender_id} · ${formatTs(msg.timestamp)}`;
        const bubble = document.createElement('div');
        bubble.className = 'bubble';
        renderMessageContent(bubble, msg.local_message);
        finalizeMessageBubbleLayout(bubble);
        body.append(meta, bubble);
        row.append(selector, avatar, body);
        bindMessageSelectionEvents(row, msg);
        chat.appendChild(row);
      });
      renderSelectionToolbar();
    };

    const finalizeMessageBubbleLayout = (container) => {
      const hasMedia = Boolean(container.querySelector('.media-image-button, .media-video, .media-file, .json-card, .forward-card, audio'));
      const hasReply = Boolean(container.querySelector('.reply-card'));
      const hasText = Array.from(container.querySelectorAll('.cq-text, .message-text')).some((node) => node.textContent.trim());
      container.classList.toggle('bubble-media-only', hasMedia && !hasText && !hasReply);
      container.classList.toggle('bubble-media-mixed', hasMedia && (hasText || hasReply));
    };

    const renderMessageContent = (container, value) => {
      clearNode(container);
      const contentText = text(value);
      const hasReply = contentText.includes('[CQ:reply');
      const hasRichMedia = /\[CQ:(image|record|video|file|json|forward),/.test(contentText);
      container.classList.toggle('bubble-cq', contentText.includes('[CQ:'));
      container.classList.toggle('bubble-media-mixed', hasReply && hasRichMedia);
      if (renderCQParts(container, value)) {
        finalizeMessageBubbleLayout(container);
        return;
      }
      const cqImage = parseCQSegment(value, 'image');
      if (cqImage && cqImage.url) {
        renderImageLink(container, cqImage.url, cqImage.file || '媒体图片');
        return;
      }
      const cqJson = parseCQSegment(value, 'json');
      if (cqJson && cqJson.data) {
        renderCQJsonCard(container, cqJson.data);
        return;
      }
      if (renderLocalMediaParts(container, value)) {
        finalizeMessageBubbleLayout(container);
        return;
      }
      if (!isMedia(value)) {
        appendLinkedText(container, value, 'message-text');
        finalizeMessageBubbleLayout(container);
        return;
      }
      if (isImg(value)) {
        renderImageLink(container, value, '媒体图片');
        return;
        const img = document.createElement('img');
        img.className = 'media-img';
        img.src = normalizeSafeMediaSrc(value);
        img.alt = '媒体图片';
        container.appendChild(img);
        return;
      }
      if (isVideo(value)) {
        const src = normalizeSafeMediaSrc(value);
        if (!src) {
          renderMissingMediaChip(container, value);
          return;
        }
        const video = document.createElement('video');
        video.className = 'media-video';
        video.src = src;
        video.controls = true;
        container.appendChild(video);
        return;
      }
      if (isVoice(value)) {
        const src = normalizeSafeMediaSrc(value);
        if (!src) {
          renderMissingMediaChip(container, value);
          return;
        }
        const audio = document.createElement('audio');
        audio.src = src;
        audio.controls = true;
        container.appendChild(audio);
        return;
      }
      renderFileLink(container, value, fileName(value));
    };

    const parseCQParams = (paramsText) => {
      const params = {};
      paramsText.split(',').forEach((item) => {
        const index = item.indexOf('=');
        if (index <= 0) return;
        const key = item.slice(0, index);
        const raw = item.slice(index + 1);
        try {
          params[key] = decodeURIComponent(decodeHtmlEntities(raw));
        } catch {
          params[key] = decodeHtmlEntities(raw);
        }
      });
      return params;
    };

    const renderLocalMediaParts = (container, value) => {
      if (typeof value !== 'string' || !value.includes('/static/storage/')) return false;
      const pattern = /\/static\/storage\/[a-f0-9]{32}\.[a-z0-9]+/ig;
      let cursor = 0;
      let matched = false;
      const wrapper = document.createElement('div');
      wrapper.className = 'cq-parts';
      let match = null;
      while ((match = pattern.exec(value)) !== null) {
        matched = true;
        const before = value.slice(cursor, match.index);
        if (before.trim()) {
          appendLinkedText(wrapper, before, 'cq-text');
        }
        renderLocalMediaAsset(wrapper, match[0]);
        cursor = pattern.lastIndex;
      }
      const tail = value.slice(cursor);
      if (tail.trim()) {
        appendLinkedText(wrapper, tail, 'cq-text');
      }
      if (!matched) return false;
      container.appendChild(wrapper);
      return true;
    };

    const renderLocalMediaAsset = (container, path) => {
      if (!isLocalPath(path)) {
        renderMissingMediaChip(container, path);
        return;
      }
      if (isImg(path)) {
        renderImageLink(container, path, '媒体图片');
      } else if (isVideo(path)) {
        const src = normalizeSafeMediaSrc(path);
        if (!src) {
          renderMissingMediaChip(container, path);
          return;
        }
        const video = document.createElement('video');
        video.className = 'media-video';
        video.src = src;
        video.controls = true;
        container.appendChild(video);
      } else if (isVoice(path)) {
        const src = normalizeSafeMediaSrc(path);
        if (!src) {
          renderMissingMediaChip(container, path);
          return;
        }
        const audio = document.createElement('audio');
        audio.src = src;
        audio.controls = true;
        container.appendChild(audio);
      } else {
        renderFileLink(container, path, fileName(path));
      }
    };

    const pokeDisplayText = (target) => (
      target ? `戳一戳 @${target}` : '戳一戳'
    );

    const renderCQParts = (container, value) => {
      if (typeof value !== 'string' || !value.includes('[CQ:')) return false;
      const pattern = /\[CQ:(\w+),([\s\S]*?)\]/g;
      let cursor = 0;
      let matched = false;
      const wrapper = document.createElement('div');
      wrapper.className = 'cq-parts';

      const appendText = (textValue) => {
        if (!textValue) return;
        appendLinkedText(wrapper, textValue, 'cq-text');
      };

      let match = null;
      while ((match = pattern.exec(value)) !== null) {
        matched = true;
        appendText(value.slice(cursor, match.index));
        const kind = match[1];
        const params = parseCQParams(match[2]);
        if (kind === 'image' && params.url) {
          renderImageLink(wrapper, params.url, params.summary || params.file || '媒体图片');
        } else if (kind === 'record' && (params.url || params.file)) {
          renderLocalMediaAsset(wrapper, params.url || params.file);
        } else if (kind === 'video' && (params.url || params.file)) {
          renderLocalMediaAsset(wrapper, params.url || params.file);
        } else if (kind === 'file' && (params.url || params.file)) {
          renderFileLink(wrapper, params.url || params.file, params.name || params.file || '文件');
        } else if (kind === 'json' && params.data) {
          renderCQJsonCard(wrapper, params.data);
        } else if (kind === 'at') {
          renderCQChip(wrapper, `@${params.qq || 'unknown'}`);
        } else if (kind === 'poke') {
          renderCQChip(wrapper, pokeDisplayText(params.qq || params.id || params.user_id));
        } else if (kind === 'reply') {
          renderReplyCard(wrapper, params);
          cursor = pattern.lastIndex;
          continue;
        } else if (kind === 'forward') {
          renderForwardCard(wrapper, params);
        } else {
          renderCQChip(wrapper, `[${kind}]`);
        }
        cursor = pattern.lastIndex;
      }
      appendText(value.slice(cursor));
      if (!matched) return false;
      container.appendChild(wrapper);
      return true;
    };

    const renderImageLink = (container, src, alt) => {
      const cleanSrc = normalizeSafeMediaSrc(decodeHtmlEntities(String(src || '')));
      if (!cleanSrc || (!isLocalPath(cleanSrc) && isRemoteUrl(cleanSrc))) {
        renderMissingMediaChip(container, src);
        return;
      }
      const preview = document.createElement('button');
      preview.className = 'media-image-button';
      preview.type = 'button';
      preview.title = '预览图片';
      preview.addEventListener('click', () => openImagePreview(cleanSrc, alt));
      const img = document.createElement('img');
      img.className = 'media-img';
      img.src = cleanSrc;
      img.referrerPolicy = 'no-referrer';
      img.alt = alt;
      preview.appendChild(img);
      container.appendChild(preview);
    };

    const renderFileLink = (container, src, label) => {
      const cleanSrc = normalizeSafeMediaSrc(decodeHtmlEntities(String(src || '')));
      if (!cleanSrc || (!isLocalPath(cleanSrc) && isRemoteUrl(cleanSrc))) {
        renderMissingMediaChip(container, src);
        return;
      }
      const link = document.createElement('a');
      link.className = 'media-file';
      link.href = cleanSrc;
      link.target = isLocalPath(cleanSrc) ? '_self' : '_blank';
      link.rel = 'noreferrer';
      link.textContent = label || fileName(cleanSrc);
      container.appendChild(link);
    };

    const renderMissingMediaChip = (container, src) => {
      const chip = document.createElement('span');
      chip.className = 'media-missing';
      chip.title = decodeHtmlEntities(String(src || ''));
      chip.textContent = '媒体未缓存';
      container.appendChild(chip);
    };

    const renderCQChip = (container, label) => {
      const chip = document.createElement('span');
      chip.className = 'cq-chip';
      chip.textContent = label;
      container.appendChild(chip);
    };

    const renderReplyCard = (container, params) => {
      const card = document.createElement('button');
      card.className = 'reply-card';
      card.type = 'button';
      if (params.id) {
        card.title = '跳转到被回复的消息';
        card.addEventListener('click', () => jumpToReplyMessage(params.id));
      }
      const title = document.createElement('strong');
      title.textContent = params.id ? `回复 #${params.id}` : '回复消息';
      card.appendChild(title);
      const source = findReplyMessage(params.id);
      const preview = document.createElement('div');
      preview.className = 'reply-preview';
      const backendPreview = replyPreviewText(params.id);
      const isMissing = params.id && String(state.replyJumpMissingId || '') === String(params.id);
      preview.textContent = isMissing ? '原消息未缓存或已被清理' : (backendPreview || (source
        ? `${source.nickname || source.sender_id}: ${plainMessagePreview(source.local_message || source.raw_message)}`
        : '原消息暂未加载'));
      card.appendChild(preview);
      container.appendChild(card);
    };

    const shortForwardText = (value, maxLength = 6) => {
      const normalized = text(value).replace(/\s+/g, ' ').trim();
      if (!normalized) return '[消息]';
      if (/^\[[^\]]+\]$/.test(normalized)) return normalized;
      return normalized.length > maxLength ? `${normalized.slice(0, maxLength)}...` : normalized;
    };

    const oneBotSegmentSummary = (segment) => {
      if (!segment || typeof segment !== 'object') return text(segment);
      const type = segment.type || segment.kind || '';
      const data = segment.data || segment;
      if (type === 'text') return data.text || '';
      if (type === 'image') return '[图片]';
      if (type === 'record') return '[语音]';
      if (type === 'video') return '[视频]';
      if (type === 'file') return '[文件]';
      if (type === 'json') return '[卡片]';
      if (type === 'forward') return '[合并转发]';
      if (type === 'at') return `@${data.qq || ''}`;
      if (type === 'poke') return '[戳一戳]';
      return data.text || data.summary || (data.url || data.file ? '[媒体]' : `[${type || '消息'}]`);
    };

    const oneBotContentSummary = (content) => {
      if (Array.isArray(content)) {
        const parts = content.map((segment) => oneBotSegmentSummary(segment)).filter(Boolean);
        return parts.join(' ') || '[消息]';
      }
      if (content && typeof content === 'object') {
        return oneBotSegmentSummary(content);
      }
      const raw = text(content);
      if (/\[CQ:image,[^\]]+\]/.test(raw)) return '[图片]';
      if (/\[CQ:record,[^\]]+\]/.test(raw)) return '[语音]';
      if (/\[CQ:video,[^\]]+\]/.test(raw)) return '[视频]';
      if (/\[CQ:file,[^\]]+\]/.test(raw)) return '[文件]';
      if (/\[CQ:json,[^\]]+\]/.test(raw)) return '[卡片]';
      if (/\[CQ:forward,[^\]]+\]/.test(raw)) return '[合并转发]';
      if (/\[CQ:poke[^\]]*\]/.test(raw)) return '[戳一戳]';
      return raw
        .replace(/\[CQ:reply,[^\]]+\]/g, '')
        .replace(/\[CQ:at,qq=([^\],]+)[^\]]*\]/g, '@$1')
        .replace(/\[CQ:poke[^\]]*\]/g, '[戳一戳]')
        .replace(/\[[^\]]+\]/g, '')
        .trim() || '[消息]';
    };

    const forwardMessagesFromPayload = (payload) => {
      const data = payload && payload.data ? payload.data : payload;
      const messages = Array.isArray(data) ? data : (data && (data.messages || data.message)) || [];
      return Array.isArray(messages) ? messages : [];
    };

    const forwardSenderInfo = (item = {}) => {
      const sender = item.sender && typeof item.sender === 'object' ? item.sender : {};
      const id = sender.user_id || sender.uin || item.user_id || item.sender_id || '';
      const name = sender.nickname || sender.card || item.nickname || id || 'forward';
      return { id: String(id || name), name: String(name) };
    };

    const renderForwardSummary = (container, payload) => {
      clearNode(container);
      const messages = forwardMessagesFromPayload(payload).slice(0, 3);
      if (!messages.length) {
        const empty = document.createElement('span');
        empty.className = 'forward-summary-empty';
        empty.textContent = '暂无摘要';
        container.appendChild(empty);
        return;
      }
      messages.forEach((item) => {
        const row = document.createElement('div');
        row.className = 'forward-summary-line';
        const sender = item.sender && (item.sender.nickname || item.sender.card || item.sender.user_id);
        const content = item.raw_message || item.message || item.content || '';
        const senderText = sender ? `${shortForwardText(sender, 6)}: ` : '';
        row.textContent = `${senderText}${shortForwardText(oneBotContentSummary(content), 6)}`;
        container.appendChild(row);
      });
    };

    const loadForwardSummary = async (localPath, summary) => {
      if (!localPath) return;
      try {
        const payload = await apiGet(localPath);
        renderForwardSummary(summary, payload);
      } catch {
        summary.textContent = '摘要未缓存';
      }
    };

    const normalizeForwardParams = (params = {}) => {
      const source = params && typeof params === 'object' && params.data && typeof params.data === 'object'
        ? params.data
        : params;
      const localCandidate = source.local || source.local_path || source.path || source.url || '';
      return {
        ...source,
        id: source.id || source.forward_id || source.file || source.resid || source.resource_id || '',
        local: isLocalPath(localCandidate) ? localCandidate : (source.local || source.local_path || ''),
      };
    };

    const renderForwardCard = (container, params) => {
      const normalized = normalizeForwardParams(params);
      const card = document.createElement('div');
      card.className = 'forward-card';
      const action = document.createElement('button');
      action.className = 'btn link forward-toggle';
      action.type = 'button';
      const title = document.createElement('span');
      title.className = 'forward-title';
      title.textContent = '合并转发消息';
      if (normalized.id) action.title = `合并转发消息 ${normalized.id}`;
      const summary = document.createElement('div');
      summary.className = 'forward-summary';
      summary.textContent = normalized.local ? '正在读取摘要...' : '点击查看合并转发内容';
      action.append(title, summary);
      action.addEventListener('click', () => openForwardPreview(normalized));
      card.append(action);
      container.appendChild(card);
      loadForwardSummary(normalized.local, summary);
    };

    const loadForwardPayload = async ({ id: forwardId = '', local: localPath = '' } = {}) => {
      if (localPath) return await apiGet(localPath);
      if (!forwardId || !state.currentRobot) throw new Error('合并转发未缓存');
      return await apiGet(`/api/forward?robot_id=${encodeURIComponent(state.currentRobot.id)}&forward_id=${encodeURIComponent(forwardId)}`);
    };

    const openForwardPreview = async (normalized) => {
      const body = el('forwardPreviewBody');
      el('forwardPreviewTitle').textContent = '合并转发消息';
      el('forwardPreviewCaption').textContent = normalized.id ? `ID: ${normalized.id}` : '';
      body.textContent = '正在加载合并消息...';
      openModal('forwardPreviewModal');
      try {
        const payload = await loadForwardPayload(normalized);
        renderForwardPayload(body, payload);
        body.scrollTop = 0;
      } catch (error) {
        body.textContent = `加载失败：${error.message}`;
      }
    };

    const renderForwardPayload = (container, payload) => {
      clearNode(container);
      const messages = forwardMessagesFromPayload(payload);
      if (!Array.isArray(messages) || messages.length === 0) {
        container.textContent = '没有可显示的合并消息内容';
        return;
      }
      messages.forEach((item) => {
        const row = document.createElement('div');
        row.className = 'forward-preview-message';
        const sender = forwardSenderInfo(item);
        const avatar = document.createElement('div');
        avatar.className = 'avatar';
        renderAvatar(avatar, sender.id, 'user');
        const bodyWrap = document.createElement('div');
        bodyWrap.className = 'forward-preview-message-body';
        const meta = document.createElement('div');
        meta.className = 'forward-preview-meta';
        const time = item.time || item.timestamp || item.message_time;
        meta.textContent = time ? `${sender.name} · ${formatTs(Number(time))}` : sender.name;
        const body = document.createElement('div');
        body.className = 'forward-preview-bubble';
        const content = item.raw_message || item.message || item.content || '';
        renderOneBotContent(body, content);
        finalizeMessageBubbleLayout(body);
        bodyWrap.append(meta, body);
        row.append(avatar, bodyWrap);
        container.appendChild(row);
      });
    };

    const renderOneBotContent = (container, content) => {
      clearNode(container);
      if (Array.isArray(content)) {
        const wrapper = document.createElement('div');
        wrapper.className = 'cq-parts';
        content.forEach((segment) => renderOneBotSegment(wrapper, segment));
        container.appendChild(wrapper);
        return;
      }
      if (content && typeof content === 'object') {
        renderOneBotSegment(container, content);
        return;
      }
      renderMessageContent(container, String(content || ''));
    };

    const renderOneBotSegment = (container, segment) => {
      if (!segment || typeof segment !== 'object') {
        appendLinkedText(container, String(segment || ''), 'cq-text');
        return;
      }
      const type = segment.type || segment.kind || '';
      const data = segment.data || segment;
      if (type === 'text') {
        appendLinkedText(container, data.text || '', 'cq-text');
      } else if (type === 'image') {
        const src = data.url || data.path || data.file;
        if (src) {
          renderImageLink(container, src, data.summary || data.file || '媒体图片');
        } else {
          renderCQChip(container, '[图片]');
        }
      } else if (type === 'record') {
        const src = data.url || data.path || data.file;
        if (src) {
          renderLocalMediaAsset(container, src);
        } else {
          renderCQChip(container, '[语音]');
        }
      } else if (type === 'video') {
        const src = data.url || data.path || data.file;
        if (src) {
          renderLocalMediaAsset(container, src);
        } else {
          renderCQChip(container, '[视频]');
        }
      } else if (type === 'file') {
        const src = data.url || data.path || data.file;
        if (src) {
          renderFileLink(container, src, data.name || data.file || '文件');
        } else {
          renderCQChip(container, '[文件]');
        }
      } else if (type === 'at') {
        renderCQChip(container, `@${data.qq || 'unknown'}`);
      } else if (type === 'poke') {
        renderCQChip(container, pokeDisplayText(data.qq || data.id || data.user_id));
      } else if (type === 'reply') {
        renderReplyCard(container, data);
      } else if (type === 'json') {
        renderCQJsonCard(container, data.data || '{}');
      } else if (type === 'forward') {
        renderForwardCard(container, data);
      } else if (data.text) {
        appendLinkedText(container, data.text, 'cq-text');
      } else if (data.url || data.file) {
        renderImageLink(container, data.url || data.file, data.summary || data.file || '媒体');
      } else {
        renderCQChip(container, `[${type || '消息'}]`);
      }
    };

    const parseCQSegment = (value, kind) => {
      if (typeof value !== 'string') return null;
      const match = value.trim().match(new RegExp(`^\\[CQ:${kind},([\\s\\S]*)\\]$`));
      if (!match) return null;
      return parseCQParams(match[1]);
    };

    const normalizeCardUrl = (url) => {
      return normalizeSafeUrl(url, { allowLocal: true });
    };

    const cardPageUrlPriority = {
      qqdocurl: 0,
      docurl: 0,
      weburl: 1,
      webpageurl: 1,
      targeturl: 2,
      jumpurl: 2,
      shareurl: 3,
      pageurl: 3,
      contenturl: 3,
      link: 4,
      url: 5,
    };

    const normalizeCardFieldKey = (key) => String(key || '').toLowerCase().replace(/[^a-z0-9]/g, '');

    const isQqMiniappShellUrl = (url) => {
      const normalized = normalizeCardUrl(url || '');
      if (!normalized || isLocalPath(normalized)) return false;
      try {
        const parsed = new URL(normalized, window.location.origin);
        const host = parsed.hostname.toLowerCase();
        const path = parsed.pathname.toLowerCase();
        return ['m.q.qq.com', 'q.qq.com'].includes(host) && (path.startsWith('/a/s/') || path.includes('miniapp'));
      } catch {
        return false;
      }
    };

    const collectCardPageUrls = (value, key = '', results = []) => {
      if (!value) return results;
      if (typeof value === 'string') {
        const normalizedKey = normalizeCardFieldKey(key);
        if (Object.prototype.hasOwnProperty.call(cardPageUrlPriority, normalizedKey)) {
          const normalizedUrl = normalizeCardUrl(value);
          if (normalizedUrl && !isLocalPath(normalizedUrl) && !/\.(png|jpe?g|gif|webp|bmp|svg|mp[34]|m4a|wav|ogg|silk|amr|mov|mkv|avi)([?#].*)?$/i.test(normalizedUrl)) {
            results.push({ url: normalizedUrl, key: normalizedKey });
          }
        }
        return results;
      }
      if (Array.isArray(value)) {
        value.forEach((item) => collectCardPageUrls(item, key, results));
        return results;
      }
      if (typeof value === 'object') {
        Object.entries(value).forEach(([childKey, childValue]) => collectCardPageUrls(childValue, childKey, results));
      }
      return results;
    };

    const pickPreferredCardPageUrl = (...sources) => {
      const seen = new Set();
      const candidates = [];
      sources.forEach((source) => {
        collectCardPageUrls(source).forEach((candidate) => {
          if (seen.has(candidate.url)) return;
          seen.add(candidate.url);
          candidates.push(candidate);
        });
      });
      candidates.sort((left, right) => {
        const leftShell = isQqMiniappShellUrl(left.url) ? 1 : 0;
        const rightShell = isQqMiniappShellUrl(right.url) ? 1 : 0;
        if (leftShell !== rightShell) return leftShell - rightShell;
        const leftKey = cardPageUrlPriority[left.key] ?? 9;
        const rightKey = cardPageUrlPriority[right.key] ?? 9;
        if (leftKey !== rightKey) return leftKey - rightKey;
        return left.url.length - right.url.length;
      });
      return candidates[0] ? candidates[0].url : '';
    };

    const renderCQJsonCard = (container, data) => {
      let payload = null;
      try {
        payload = JSON.parse(data);
      } catch {
        container.textContent = data;
        return;
      }
      const detail = payload.meta && (payload.meta.detail_1 || Object.values(payload.meta)[0]);
      const title = detail && detail.title ? detail.title : payload.prompt || payload.app || 'JSON 卡片';
      const desc = detail && detail.desc ? detail.desc : payload.prompt || '';
      const url = pickPreferredCardPageUrl(detail, payload);
      const localPage = detail && detail.local_page;
      const preview = detail && (detail.preview || detail.icon);

      const hasMiniappShell = collectCardPageUrls(detail || payload).some((candidate) => isQqMiniappShellUrl(candidate.url));
      const shouldPreferDirectUrl = url && hasMiniappShell && !isQqMiniappShellUrl(url);
      const cleanUrl = normalizeCardUrl((shouldPreferDirectUrl ? url : localPage) || url || '');
      const originalUrl = normalizeCardUrl(url || '');
      const card = document.createElement(cleanUrl ? 'a' : 'div');
      card.className = 'json-card';
      if (cleanUrl) {
        card.href = cleanUrl;
        card.target = '_blank';
        card.rel = 'noreferrer';
        if (localPage && originalUrl) card.dataset.originalUrl = originalUrl;
        card.title = localPage && originalUrl ? `打开本地快照：${originalUrl}` : '打开卡片网页';
      }
      if (preview) {
        const previewSrc = normalizeCardUrl(preview);
        if (previewSrc && (isLocalPath(previewSrc) || !isRemoteUrl(previewSrc))) {
          const img = document.createElement('img');
          img.className = 'json-card-img';
          img.src = previewSrc;
          img.alt = title;
          card.appendChild(img);
        } else {
          const missing = document.createElement('span');
          missing.className = 'media-missing';
          missing.textContent = '预览未缓存';
          missing.title = previewSrc;
          card.appendChild(missing);
        }
      }
      const body = document.createElement('div');
      body.className = 'json-card-body';
      const heading = document.createElement('div');
      heading.className = 'json-card-title';
      heading.textContent = title;
      const summary = document.createElement('div');
      summary.className = 'json-card-desc';
      summary.textContent = desc;
      body.append(heading, summary);
      card.appendChild(body);
      container.appendChild(card);
    };

    const renderAll = () => {
      renderDashboard();
      renderAccounts();
      renderSettings();
      renderSearchResults();
      renderRooms();
      renderChat();
    };

    const loadAdapters = async () => {
      const [dashboard, backupStatus, bots, adapters] = await Promise.all([apiGet('/api/dashboard'), apiGet('/api/backup/status'), apiGet('/api/bots'), apiGet('/api/adapters')]);
      state.dashboard = dashboard;
      state.backupStatus = backupStatus;
      state.accountList = bots;
      state.adapterList = adapters;
      await refreshAuthIdentity();
      await refreshAdminTokens();
      await refreshAdminUsers();
      await refreshAdminSessions();
      const restored = await restoreRouteState();
      if (restored) return;
      if (state.accountList.length > 0 && !state.currentRobot) {
        await switchAccount(state.accountList[0]);
      } else {
        renderAll();
      }
    };

    async function switchAccount(acc, { updateRoute = true } = {}) {
      state.selectionMode = false;
      state.selectedMessageHashes.clear();
      state.currentRobot = acc;
      state.currentRoom = null;
      state.messageList = [];
      if (updateRoute) writeRouteState();
      pushUiLog(`切换账号：${acc.display_name || acc.id}`);
      clearSearch();
      state.loadingRooms = true;
      renderAll();
      try {
        const [rooms, captureTargets] = await Promise.all([
          apiGet(`/api/rooms?robot_id=${encodeURIComponent(acc.id)}`),
          apiGet(`/api/bots/${encodeURIComponent(acc.id)}/capture-targets`),
        ]);
        state.roomList = rooms;
        state.captureTargetList = captureTargets;
        state.capturePolicyError = '';
      } catch (error) {
        state.capturePolicyError = error.message || String(error);
        throw error;
      } finally {
        state.loadingRooms = false;
        renderAll();
      }
    }

    const parseAdapterConfig = (raw) => {
      const value = text(raw).trim();
      if (!value) return {};
      try {
        const parsed = JSON.parse(value);
        return parsed && typeof parsed === 'object' && !Array.isArray(parsed) ? parsed : {};
      } catch (_) {
        return {};
      }
    };

    const firstConfigValue = (config, keys) => {
      for (const key of keys) {
        if (config[key] !== undefined && config[key] !== null && config[key] !== '') return config[key];
      }
      return '';
    };

    const fillAdapterQuickConfig = (raw) => {
      const config = parseAdapterConfig(raw);
      el('adapterWsHost').value = firstConfigValue(config, ['reverse_ws_host', 'ws_host', 'host']);
      el('adapterWsPort').value = firstConfigValue(config, ['reverse_ws_port', 'ws_port', 'port']);
      el('adapterWsToken').value = firstConfigValue(config, ['reverse_ws_token', 'token', 'access_token']);
    };

    const syncAdapterConfigFromQuickFields = (config = parseAdapterConfig(el('adapterConfig').value)) => {
      const host = el('adapterWsHost').value.trim();
      const port = el('adapterWsPort').value.trim();
      const token = el('adapterWsToken').value.trim();
      if (host && (host.length > 255 || !/^[A-Za-z0-9_.:-]+$/.test(host))) {
        return validationError('adapter host is invalid');
      }
      if (port) {
        const parsedPort = normalizedInteger(port, 'adapter port', { min: 1, max: 65535 });
        if (parsedPort === null) return null;
        config.reverse_ws_port = parsedPort;
      } else {
        delete config.reverse_ws_port;
      }
      if (token && (token.length > 1024 || /[\r\n]/.test(token))) {
        return validationError('adapter token is invalid');
      }
      if (host) config.reverse_ws_host = host;
      else delete config.reverse_ws_host;
      if (token) config.token = token;
      else delete config.token;
      el('adapterConfig').value = Object.keys(config).length ? JSON.stringify(config, null, 2) : '';
      return config;
    };

    const setAdapterStatusFromEnabled = () => {
      el('adapterStatus').value = el('adapterEnabledSwitch').checked ? 'green' : 'gray';
    };

    const resetAdapterForm = () => {
      state.editingAdapterId = null;
      state.adapterEditorOpen = false;
      el('adapterId').value = '';
      el('adapterPlatform').value = 'qq';
      el('adapterStatus').value = 'gray';
      el('adapterConfig').value = '';
      el('adapterEnabledSwitch').checked = false;
      fillAdapterQuickConfig('');
      renderSettings();
    };

    const openNewAdapter = () => {
      state.editingAdapterId = null;
      state.adapterEditorOpen = true;
      el('adapterId').value = '';
      el('adapterPlatform').value = 'qq';
      el('adapterStatus').value = 'gray';
      el('adapterConfig').value = '';
      el('adapterEnabledSwitch').checked = false;
      fillAdapterQuickConfig('');
      renderSettings();
      resetAdapterEditorScroll();
    };

    const editAdapter = (adapter) => {
      state.editingAdapterId = adapter.id;
      state.adapterEditorOpen = true;
      el('adapterId').value = adapter.id;
      el('adapterPlatform').value = adapter.platform;
      el('adapterStatus').value = adapter.status;
      el('adapterConfig').value = adapter.config_json || '';
      el('adapterEnabledSwitch').checked = adapter.status === 'green';
      fillAdapterQuickConfig(adapter.config_json || '');
      renderSettings();
      resetAdapterEditorScroll();
    };

    const saveAdapter = async () => {
      setAdapterStatusFromEnabled();
      const adapterId = normalizedIdentifier(el('adapterId').value, 'adapter id');
      if (adapterId === null) return;
      const platform = normalizedIdentifier(el('adapterPlatform').value, 'adapter platform', { max: 20 });
      if (platform === null) return;
      const status = normalizedChoice(el('adapterStatus').value, SAFE_ADAPTER_STATUSES, 'adapter status');
      if (status === null) return;
      const config = parseJsonObjectInput(el('adapterConfig').value, 'adapter config');
      if (config === null) return;
      const mergedConfig = syncAdapterConfigFromQuickFields(config);
      if (mergedConfig === null) return;
      const payload = {
        platform,
        status,
        config_json: Object.keys(mergedConfig).length ? JSON.stringify(mergedConfig, null, 2) : null,
      };
      if (state.editingAdapterId) {
        await apiSend(`/api/adapters/${encodeURIComponent(state.editingAdapterId)}`, 'PATCH', payload);
      } else {
        await apiSend('/api/adapters', 'POST', { id: adapterId, ...payload });
      }
      resetAdapterForm();
      await loadAdapters();
    };

    const toggleAdapterEnabled = async (adapter, enabled) => {
      const nextStatus = enabled ? 'green' : 'gray';
      await apiSend(`/api/adapters/${encodeURIComponent(adapter.id)}`, 'PATCH', {
        platform: adapter.platform,
        status: nextStatus,
        config_json: adapter.config_json || null,
      });
      await loadAdapters();
    };

    const deleteAdapter = async (adapterId) => {
      await apiSend(`/api/adapters/${encodeURIComponent(adapterId)}`, 'DELETE');
      if (state.editingAdapterId === adapterId) resetAdapterForm();
      await loadAdapters();
    };

    async function runManualBackup() {
      state.backupRunResult = await apiSend('/api/backup/run', 'POST');
      state.backupStatus = await apiGet('/api/backup/status');
      state.dashboard = await apiGet('/api/dashboard');
      renderAll();
    }

    async function saveBackupSettings() {
      syncBackupCronFromControls();
      const cron = normalizeCronValue(el('backupCronInput').value);
      if (cron === null) return;
      const keepLatest = normalizedInteger(el('backupKeepLatestInput').value, 'backup keep_latest', { min: 0, max: 365 });
      if (keepLatest === null) return;
      const payload = {
        cron,
        keep_latest: keepLatest,
      };
      state.backupSettingsSaving = true;
      renderSettings();
      try {
        state.backupStatus = await apiSend('/api/backup/settings', 'PATCH', payload);
      } finally {
        state.backupSettingsSaving = false;
        renderSettings();
      }
    }

    async function resetBackupSettings() {
      state.backupSettingsSaving = true;
      renderSettings();
      try {
        state.backupStatus = await apiSend('/api/backup/settings', 'PATCH', { reset_to_env: true });
      } finally {
        state.backupSettingsSaving = false;
        renderSettings();
      }
    }

    async function refreshAdminTokens() {
      try {
        state.adminTokenList = await apiGet('/api/admin/tokens');
        state.adminTokenError = '';
      } catch (error) {
        state.adminTokenList = [];
        state.adminTokenError = error.message || String(error);
      }
    }

    async function refreshAdminUsers() {
      try {
        state.adminUserList = await apiGet('/api/admin/users');
        state.adminUserError = '';
      } catch (error) {
        state.adminUserList = [];
        state.adminUserError = error.message || String(error);
      }
    }

    async function refreshAdminSessions() {
      try {
        state.adminSessionList = await apiGet('/api/admin/sessions');
        state.adminSessionError = '';
      } catch (error) {
        state.adminSessionList = [];
        state.adminSessionError = error.message || String(error);
      }
      renderSettings();
    }

    async function refreshAuthIdentity() {
      if (!state.adminApiToken) {
        state.authIdentity = null;
        state.authError = '';
        renderSettings();
        return;
      }
      try {
        state.authIdentity = await apiGet('/api/auth/me');
        state.authError = '';
      } catch (error) {
        state.authIdentity = null;
        state.authError = error.message || String(error);
      }
      renderSettings();
    }

    async function refreshCapturePolicies() {
      if (!state.currentRobot) {
        state.captureTargetList = [];
        state.capturePolicyError = '';
        renderSettings();
        return;
      }
      try {
        state.captureTargetList = await apiGet(`/api/bots/${encodeURIComponent(state.currentRobot.id)}/capture-targets`);
        state.capturePolicyError = '';
      } catch (error) {
        state.captureTargetList = [];
        state.capturePolicyError = error.message || String(error);
      }
      renderSettings();
    }

    async function saveCapturePolicy(target, row) {
      const listMode = normalizedChoice(row.querySelector('[data-field="list_mode"]').value, SAFE_CAPTURE_LIST_MODES, 'capture list mode');
      if (listMode === null) return;
      const payload = {
        list_mode: listMode,
        capture_text: row.querySelector('[data-field="capture_text"]').checked,
        capture_image: row.querySelector('[data-field="capture_image"]').checked,
        capture_voice: row.querySelector('[data-field="capture_voice"]').checked,
        capture_video: row.querySelector('[data-field="capture_video"]').checked,
        capture_file: row.querySelector('[data-field="capture_file"]').checked,
      };
      await apiSend(
        `/api/bots/${encodeURIComponent(state.currentRobot.id)}/capture-policies/${encodeURIComponent(target.target_type)}/${encodeURIComponent(target.target_id)}`,
        'PUT',
        payload,
      );
      await refreshCapturePolicies();
    }

    async function resetCapturePolicy(target) {
      await apiSend(
        `/api/bots/${encodeURIComponent(state.currentRobot.id)}/capture-policies/${encodeURIComponent(target.target_type)}/${encodeURIComponent(target.target_id)}`,
        'DELETE',
      );
      await refreshCapturePolicies();
    }

    async function loginWithPassword() {
      const username = window.prompt('username');
      if (!username) return;
      const password = window.prompt('password');
      if (!password) return;
      const response = await fetch('/api/auth/login', {
        method: 'POST',
        headers: csrfHeaders('POST', { 'Content-Type': 'application/json' }),
        body: JSON.stringify({ username, password }),
      });
      if (!response.ok) {
        state.authError = await responseErrorMessage(response, '/api/auth/login');
        renderSettings();
        return;
      }
      const payload = await response.json();
      state.adminApiToken = payload.token;
      localStorage.setItem('chatAuditAdminApiToken', state.adminApiToken);
      state.authIdentity = { ...payload.user, actor: `db-user:${payload.user.username}` };
      state.authError = '';
      await loadAdapters();
    }

    async function logoutAuth() {
      if (state.adminApiToken) {
        try {
          await apiSend('/api/auth/logout', 'POST');
        } catch {
          // Logging out should still clear local state if the remote session is already gone.
        }
      }
      state.adminApiToken = '';
      localStorage.removeItem('chatAuditAdminApiToken');
      state.authIdentity = null;
      state.authError = '';
      renderAll();
    }

    async function createAdminToken() {
      const name = text(el('adminTokenName').value).trim();
      if (!name) return validationError('token name is required');
      if (name.length > 128 || /[\r\n]/.test(name)) return validationError('token name is invalid');
      const role = normalizedChoice(el('adminTokenRole').value, SAFE_ROLES, 'token role');
      if (role === null) return;
      state.adminTokenCreateResult = await apiSend('/api/admin/tokens', 'POST', { name, role });
      el('adminTokenName').value = '';
      await refreshAdminTokens();
      renderSettings();
    }

    async function createAdminUser() {
      const username = normalizedIdentifier(el('adminUsername').value, 'username', { max: 64, pattern: SAFE_USERNAME_PATTERN });
      if (username === null) return;
      const password = text(el('adminPassword').value);
      if (password.length < 8 || password.length > 256) return validationError('password must be 8-256 characters');
      const displayName = text(el('adminDisplayName').value).trim();
      if (displayName.length > 128 || /[\r\n]/.test(displayName)) return validationError('display name is invalid');
      const role = normalizedChoice(el('adminUserRole').value, SAFE_ROLES, 'user role');
      if (role === null) return;
      const payload = {
        username,
        password,
        display_name: displayName || null,
        role,
      };
      try {
        state.adminUserCreateResult = await apiSend('/api/admin/users', 'POST', payload);
        state.adminUserError = '';
        el('adminUsername').value = '';
        el('adminPassword').value = '';
        el('adminDisplayName').value = '';
        await refreshAdminUsers();
      } catch (error) {
        state.adminUserCreateResult = null;
        state.adminUserError = error.message || String(error);
      }
      renderSettings();
    }

    async function revokeAdminUser(userId) {
      await apiSend(`/api/admin/users/${encodeURIComponent(userId)}`, 'DELETE');
      state.adminUserCreateResult = null;
      await refreshAdminUsers();
      await refreshAdminSessions();
      renderSettings();
    }

    async function resetAdminUserPassword(userId) {
      const password = window.prompt('请输入新密码（至少 8 位）');
      if (!password) return;
      if (password.length < 8 || password.length > 256) {
        validationError('password must be 8-256 characters');
        return;
      }
      await apiSend(`/api/admin/users/${encodeURIComponent(userId)}/password`, 'POST', { password });
      state.adminUserCreateResult = null;
      await refreshAdminUsers();
      await refreshAdminSessions();
      await refreshAuthIdentity();
      renderSettings();
    }

    async function revokeAdminSession(sessionId) {
      await apiSend(`/api/admin/sessions/${encodeURIComponent(sessionId)}`, 'DELETE');
      await refreshAdminSessions();
      await refreshAuthIdentity();
      renderSettings();
    }

    async function revokeAdminToken(tokenId) {
      await apiSend(`/api/admin/tokens/${encodeURIComponent(tokenId)}`, 'DELETE');
      state.adminTokenCreateResult = null;
      await refreshAdminTokens();
      renderSettings();
    }

    async function selectRoom(room, { updateRoute = true } = {}) {
      state.selectionMode = false;
      state.selectedMessageHashes.clear();
      state.currentRoom = room;
      state.messageList = [];
      if (updateRoute) writeRouteState();
      pushUiLog(`打开会话：${roomDisplayName(room)}`);
      renderAll();
      await reloadCurrentRoom();
    }

    async function selectSearchResult(result) {
      let room = state.roomList.find((item) => item.room_id === result.room_id);
      if (!room) {
        room = { room_id: result.room_id, last_timestamp: result.timestamp };
        state.roomList = [room, ...state.roomList];
      }
      state.selectionMode = false;
      state.selectedMessageHashes.clear();
      state.currentRoom = room;
      state.messageList = [result];
      writeRouteState();
      renderAll();
      scrollChatToBottom();
    }

    async function performSearch() {
      const keyword = el('searchInput').value.trim();
      if (!state.currentRobot || !keyword) return;
      state.searchMode = true;
      pushUiLog(`搜索消息：${keyword}`);
      state.searchResults = await apiGet(`/api/search?robot_id=${encodeURIComponent(state.currentRobot.id)}&keyword=${encodeURIComponent(keyword)}&limit=50`);
      renderAll();
    }

    function clearSearch() {
      state.searchMode = false;
      state.searchResults = [];
      renderAll();
    }

    const loadMessages = async (beforeTimestamp = null, aroundMessageId = '') => {
      if (!state.currentRobot || !state.currentRoom) return [];
      let url = `/api/messages?robot_id=${encodeURIComponent(state.currentRobot.id)}&room_id=${encodeURIComponent(state.currentRoom.room_id)}&limit=50`;
      if (beforeTimestamp !== null && beforeTimestamp !== undefined) url += `&before_timestamp=${encodeURIComponent(beforeTimestamp)}`;
      if (aroundMessageId) url += `&around_message_id=${encodeURIComponent(aroundMessageId)}`;
      return await apiGet(url);
    };

    const mergeMessages = (messages) => {
      const byKey = new Map();
      messages.forEach((msg) => {
        const key = msg.msg_hash || msg.external_message_id || `${msg.timestamp}:${msg.sender_id}:${msg.raw_message}`;
        byKey.set(key, msg);
      });
      return Array.from(byKey.values()).sort((a, b) => (a.timestamp - b.timestamp) || String(a.msg_hash || '').localeCompare(String(b.msg_hash || '')));
    };

    const scrollToExternalMessage = (externalMessageId) => {
      const chat = el('chatWindow');
      const row = Array.from(chat.querySelectorAll('.message')).find((item) => String(item.dataset.externalMessageId || '') === String(externalMessageId));
      if (!row) return false;
      state.highlightedMessageId = String(externalMessageId);
      renderChat();
      requestAnimationFrame(() => {
        const highlighted = Array.from(chat.querySelectorAll('.message')).find((item) => String(item.dataset.externalMessageId || '') === String(externalMessageId));
        if (highlighted) highlighted.scrollIntoView({ block: 'center', behavior: 'smooth' });
        window.setTimeout(() => {
          if (state.highlightedMessageId === String(externalMessageId)) {
            state.highlightedMessageId = '';
            renderChat();
          }
        }, 1600);
      });
      return true;
    };

    async function jumpToReplyMessage(replyId) {
      if (!replyId || !state.currentRobot || !state.currentRoom) return;
      if (findReplyMessage(replyId)) {
        state.replyJumpMissingId = '';
        scrollToExternalMessage(replyId);
        return;
      }
      const around = await loadMessages(null, replyId);
      if (around.length > 0) {
        state.messageList = mergeMessages([...state.messageList, ...around]);
        state.replyJumpMissingId = '';
        renderChat();
        scrollToExternalMessage(replyId);
      } else {
        state.replyJumpMissingId = String(replyId);
        renderChat();
      }
    }

    async function reloadCurrentRoom() {
      if (!state.currentRoom) return;
      pushUiLog(`刷新会话：${roomDisplayName(state.currentRoom)}`);
      state.messageList = await loadMessages();
      renderAll();
      scrollChatToBottom();
    }

    const scrollChatToBottom = () => {
      const chat = el('chatWindow');
      chat.scrollTop = chat.scrollHeight;
    };

    async function handleWindowScroll() {
      const chat = el('chatWindow');
      if (!chat || chat.scrollTop !== 0 || state.loadingHistory || state.messageList.length === 0) return;
      state.loadingHistory = true;
      const oldScrollHeight = chat.scrollHeight;
      const cursorTimestamp = state.messageList[0].timestamp;
      renderChat();
      try {
        const older = await loadMessages(cursorTimestamp);
        if (older.length > 0) {
          state.messageList = [...older, ...state.messageList];
          renderChat();
          chat.scrollTop = chat.scrollHeight - oldScrollHeight;
        }
      } finally {
        state.loadingHistory = false;
        renderChat();
      }
    }

    const openExportDialog = () => {
      el('exportRobotId').value = state.currentRobot ? state.currentRobot.id : '';
      el('exportRoomId').value = state.currentRoom ? state.currentRoom.room_id : '';
      el('exportStartTimestamp').value = '';
      el('exportEndTimestamp').value = '';
      pushUiLog('打开高级过滤导出');
      openModal('exportModal');
    };

    async function downloadExportPackage() {
      const robotId = normalizedIdentifier(el('exportRobotId').value, 'export robot_id');
      if (robotId === null) return;
      const roomId = normalizedIdentifier(el('exportRoomId').value, 'export room_id', { required: false });
      if (roomId === null) return;
      const startTimestamp = normalizedTimestamp(el('exportStartTimestamp').value, 'start timestamp');
      if (startTimestamp === null) return;
      const endTimestamp = normalizedTimestamp(el('exportEndTimestamp').value, 'end timestamp');
      if (endTimestamp === null) return;
      const params = new URLSearchParams();
      [
        ['robot_id', robotId],
        ['room_id', roomId],
        ['start_timestamp', startTimestamp],
        ['end_timestamp', endTimestamp],
      ].forEach(([key, value]) => {
        if (text(value).trim()) params.append(key, text(value).trim());
      });
      params.set('compressed', 'true');
      const blob = await requestBlob(`/api/export?${params.toString()}`);
      const link = document.createElement('a');
      const objectUrl = URL.createObjectURL(blob);
      link.href = objectUrl;
      link.download = `chat-audit-export-${Date.now()}.json.gz`;
      document.body.appendChild(link);
      link.click();
      link.remove();
      URL.revokeObjectURL(objectUrl);
      closeModal('exportModal');
    }

    const openImportDialog = () => {
      el('importPackageText').value = '';
      state.importValidationReport = null;
      renderImportReport();
      pushUiLog('打开导入 JSON');
      openModal('importModal');
    };

    const parseImportPackage = () => {
      const raw = text(el('importPackageText').value).trim();
      if (!raw) return validationError('import package is required');
      if (utf8ByteLength(raw) > MAX_IMPORT_PACKAGE_BYTES) return validationError('import package is too large');
      try {
        const parsed = JSON.parse(raw);
        if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return validationError('import package must be a JSON object');
        return parsed;
      } catch {
        return validationError('import package is not valid JSON');
      }
    };

    async function validateImportPackage() {
      if (!el('importPackageText').value.trim()) return;
      const packageJson = parseImportPackage();
      if (packageJson === null) return;
      state.importValidationReport = await apiSend('/api/import/validate', 'POST', packageJson);
      renderImportReport();
    }

    async function submitImportPackage() {
      if (!state.importValidationReport || !state.importValidationReport.valid) return;
      const packageJson = parseImportPackage();
      if (packageJson === null) return;
      await apiSend('/api/import', 'POST', packageJson);
      closeModal('importModal');
      if (state.currentRobot) await switchAccount(state.currentRobot);
    }

    const renderImportReport = () => {
      const box = el('importValidationReport');
      clearNode(box);
      const report = state.importValidationReport;
      box.hidden = !report;
      if (!report) return;
      const rows = [
        ['校验结果', report.valid ? '通过' : '失败', report.valid ? 'ok' : 'bad'],
        ['schema', report.schema || '-'],
        ['checksum', report.checksum_valid === null ? '未提供' : (report.checksum_valid ? '通过' : '失败')],
        ['signature', report.signature_valid === null ? '未提供' : (report.signature_valid ? '通过' : '失败'), report.signature_valid === false ? 'bad' : undefined],
        ['source', report.source ? `${report.source.system || '-'} / ${report.source.instance_id || '-'}` : '-'],
        ['counts', `messages=${report.counts.messages} / robot_messages=${report.counts.robot_messages} / media_assets=${report.counts.media_assets} / media_files=${report.counts.media_files || 0}`],
      ];
      if (report.diff && report.diff.messages) {
        rows.push(['diff.messages 新增/更新/不变', `${report.diff.messages.new} / ${report.diff.messages.update} / ${report.diff.messages.unchanged}`]);
      }
      if (report.diff && report.diff.robot_messages) {
        rows.push(['diff.robot_messages 新增/已存在', `${report.diff.robot_messages.new} / ${report.diff.robot_messages.existing}`]);
      }
      if (report.diff && report.diff.media_assets) {
        rows.push(['diff.media_assets 新增/更新/不变', `${report.diff.media_assets.new} / ${report.diff.media_assets.update} / ${report.diff.media_assets.unchanged}`]);
      }
      if (report.media_files) {
        rows.push(['媒体文件 media_files checked/missing/mismatch', `${report.media_files.checked} / ${report.media_files.missing} / ${report.media_files.mismatch}`]);
      }
      if (report.errors && report.errors.length) rows.push(['errors', report.errors.join('; '), 'bad']);
      rows.forEach(([name, value, className]) => {
        const row = document.createElement('div');
        const label = document.createElement('strong');
        label.textContent = `${name}：`;
        const valueNode = document.createElement('span');
        if (className) valueNode.className = className;
        valueNode.textContent = value;
        row.append(label, valueNode);
        box.appendChild(row);
      });
    };

    const openOfflineAuditDialog = async () => {
      state.offlineAuditReport = null;
      state.offlineRepairReport = null;
      renderOfflineAuditReport();
      pushUiLog('打开离线验收');
      openModal('offlineAuditModal');
      await runOfflineAudit();
    };

    async function runOfflineAudit() {
      const params = new URLSearchParams({ limit: '50000' });
      if (state.currentRobot) params.append('robot_id', state.currentRobot.id);
      if (state.currentRoom) params.append('room_id', state.currentRoom.room_id);
      state.offlineAuditReport = await apiGet(`/api/offline/audit?${params.toString()}`);
      renderOfflineAuditReport();
    }

    async function repairOfflineAudit() {
      const params = new URLSearchParams({ limit: '50000' });
      state.offlineRepairReport = await apiSend(`/api/offline/repair?${params.toString()}`, 'POST');
      await runOfflineAudit();
    }

    const renderOfflineAuditReport = () => {
      const box = el('offlineAuditReport');
      clearNode(box);
      const report = state.offlineAuditReport;
      if (!report) {
        box.textContent = '正在验收本地缓存...';
        return;
      }
      const rows = [
        ['验收结果', report.offline_ready ? '通过' : '存在未缓存项', report.offline_ready ? 'ok' : 'bad'],
        ['messages', report.messages_scanned],
        ['media_assets', report.media_assets_checked],
        ['profile_avatars', `${report.profile_avatars_checked} / ${report.missing_profile_avatars}`],
        ['remote_media_urls', report.remote_media_urls],
        ['uncached_card_pages', report.uncached_card_pages],
        ['uncached_forwards', report.uncached_forwards],
        ['missing_media_assets', report.missing_media_assets],
        ['missing_media_files', report.missing_media_files],
      ];
      rows.forEach(([name, value, className]) => {
        const row = document.createElement('div');
        const label = document.createElement('strong');
        label.textContent = `${name}：`;
        const valueNode = document.createElement('span');
        if (className) valueNode.className = className;
        valueNode.textContent = value;
        row.append(label, valueNode);
        box.appendChild(row);
      });
      if (state.offlineRepairReport) {
        const repaired = state.offlineRepairReport;
        const row = document.createElement('div');
        const label = document.createElement('strong');
        label.textContent = 'repair: ';
        const valueNode = document.createElement('span');
        valueNode.textContent = `assets ${repaired.repaired_media_assets}, files ${repaired.repaired_media_files}, sizes ${repaired.repaired_file_sizes}, profile avatars ${repaired.repaired_profile_avatars}`;
        row.append(label, valueNode);
        box.appendChild(row);
      }
      if (report.reason_summary && Object.keys(report.reason_summary).length) {
        const summary = document.createElement('div');
        const label = document.createElement('strong');
        label.textContent = '缺失原因汇总：';
        const valueNode = document.createElement('span');
        valueNode.textContent = Object.entries(report.reason_summary)
          .map(([reason, count]) => `${reason} ${count}`)
          .join(' / ');
        summary.append(label, valueNode);
        box.appendChild(summary);
      }
      (report.issues || []).slice(0, 20).forEach((issue) => {
        const row = document.createElement('div');
        row.className = 'bad';
        const title = issue.label || issue.reason;
        const action = issue.action ? `；${issue.action}` : '';
        row.textContent = `${issue.kind}: ${title} - ${issue.target}${action}`;
        box.appendChild(row);
      });
    };

    const openModal = (id) => el(id).classList.add('open');
    const closeModal = (id) => {
      el(id).classList.remove('open');
      if (id === 'imagePreviewModal') {
        el('imagePreviewImg').removeAttribute('src');
        el('imagePreviewCaption').textContent = '';
      }
      if (id === 'forwardPreviewModal') {
        el('forwardPreviewCaption').textContent = '';
        clearNode(el('forwardPreviewBody'));
      }
    };

    const closeFloatingPanels = () => {
      el('topLogCard').classList.remove('open');
      el('paletteToggleButton').classList.remove('active');
      el('paletteToggleButton').closest('.palette-card').classList.remove('open');
    };

    const toggleSettingsPage = () => {
      state.settingsMode = !state.settingsMode;
      if (!state.settingsMode) {
        state.adapterEditorOpen = false;
        state.editingAdapterId = null;
      }
      closeFloatingPanels();
      renderSettings();
      pushUiLog(state.settingsMode ? '打开设置页面' : '关闭设置页面');
    };

    const closeSettingsPage = () => {
      state.settingsMode = false;
      state.adapterEditorOpen = false;
      state.editingAdapterId = null;
      renderSettings();
      pushUiLog('关闭设置页面');
    };

    const openLogDialog = () => {
      renderActivityLogs();
      el('topLogCard').classList.toggle('open');
      el('paletteToggleButton').classList.remove('active');
      el('paletteToggleButton').closest('.palette-card').classList.remove('open');
    };

    const togglePalettePanel = () => {
      const card = el('paletteToggleButton').closest('.palette-card');
      const open = !card.classList.contains('open');
      card.classList.toggle('open', open);
      el('paletteToggleButton').classList.toggle('active', open);
      el('topLogCard').classList.remove('open');
    };

    const openImagePreview = (src, alt) => {
      const cleanSrc = normalizeSafeMediaSrc(src);
      if (!cleanSrc) return;
      el('imagePreviewImg').src = cleanSrc;
      el('imagePreviewImg').alt = alt || '';
      el('imagePreviewCaption').textContent = alt || src;
      openModal('imagePreviewModal');
    };

    const handleUiError = (error) => {
      const message = error && error.message ? error.message : '操作失败，请稍后重试';
      pushUiLog(message, 'error');
    };

    const guardedHandler = (handler) => (event) => {
      try {
        Promise.resolve(handler(event)).catch(handleUiError);
      } catch (error) {
        handleUiError(error);
      }
    };

    const on = (id, eventName, handler) => {
      el(id).addEventListener(eventName, guardedHandler(handler));
    };

    const onAll = (selector, eventName, handler) => {
      document.querySelectorAll(selector).forEach((node) => node.addEventListener(eventName, guardedHandler(handler)));
    };

    const bindEvents = () => {
      mountAdapterEditorPortal();
      [
        ['loginButton', loginWithPassword],
        ['logoutButton', logoutAuth],
        ['refreshAuthButton', refreshAuthIdentity],
        ['refreshCapturePoliciesButton', refreshCapturePolicies],
        ['settingsToggle', toggleSettingsPage],
        ['closeSettingsButton', closeSettingsPage],
        ['newAdapterButton', openNewAdapter],
        ['adapterEditorBackdrop', resetAdapterForm],
        ['clearAdapterButton', resetAdapterForm],
        ['saveAdapterButton', saveAdapter],
        ['runBackupButton', runManualBackup],
        ['saveBackupSettingsButton', saveBackupSettings],
        ['resetBackupSettingsButton', resetBackupSettings],
        ['createAdminTokenButton', createAdminToken],
        ['createAdminUserButton', createAdminUser],
        ['refreshAdminSessionsButton', refreshAdminSessions],
        ['searchButton', performSearch],
        ['clearSearchButton', clearSearch],
        ['refreshButton', reloadCurrentRoom],
        ['openOfflineAuditButton', openOfflineAuditDialog],
        ['runOfflineAuditButton', runOfflineAudit],
        ['repairOfflineAuditButton', repairOfflineAudit],
        ['openExportButton', openExportDialog],
        ['selectAllVisibleButton', selectAllVisibleMessages],
        ['exportSelectedButton', exportSelectedMessages],
        ['renderSelectedImageButton', renderSelectedMessagesImage],
        ['cancelSelectionButton', exitSelectionMode],
        ['paletteToggleButton', togglePalettePanel],
        ['openLogButton', openLogDialog],
        ['downloadExportButton', downloadExportPackage],
        ['openImportButton', openImportDialog],
        ['validateImportButton', validateImportPackage],
        ['submitImportButton', submitImportPackage],
      ].forEach(([id, handler]) => on(id, 'click', handler));

      on('adapterEnabledSwitch', 'change', setAdapterStatusFromEnabled);
      ['adapterWsHost', 'adapterWsPort', 'adapterWsToken'].forEach((id) => {
        on(id, 'input', syncAdapterConfigFromQuickFields);
      });
      on('adapterConfig', 'blur', (event) => fillAdapterQuickConfig(event.target.value));
      onAll('input[name="backupPreset"]', 'change', syncBackupCronFromControls);
      on('backupTimeInput', 'input', syncBackupCronFromControls);
      on('backupWeekdayInput', 'change', syncBackupCronFromControls);
      on('backupCronInput', 'input', applyBackupPresetVisibility);
      on('searchInput', 'keydown', (event) => { if (event.key === 'Enter') performSearch(); });
      on('chatWindow', 'scroll', handleWindowScroll);
      on('dayModeButton', 'click', () => setThemeMode('light'));
      on('nightModeButton', 'click', () => setThemeMode('dark'));
      on('primaryColorInput', 'input', (event) => { state.palette.primary = event.target.value; savePalette(); });
      on('secondaryColorInput', 'input', (event) => { state.palette.secondary = event.target.value; savePalette(); });
      on('contrastColorInput', 'input', (event) => { state.palette.contrast = event.target.value; savePalette(); });
      onAll('[data-close-modal]', 'click', (event) => closeModal(event.currentTarget.dataset.closeModal));
      onAll('.modal-backdrop', 'click', (event) => {
        if (event.target === event.currentTarget) closeModal(event.currentTarget.id);
      });
      document.addEventListener('pointerdown', (event) => {
        const target = event.target;
        const clickedPalette = target.closest && target.closest('.palette-card');
        const clickedLog = target.closest && target.closest('#topLogCard');
        if (!clickedPalette && !clickedLog) closeFloatingPanels();
      });
      document.addEventListener('keydown', (event) => {
        if (event.key === 'Escape') {
          if (state.selectionMode) {
            exitSelectionMode();
            return;
          }
          document.querySelectorAll('.modal-backdrop.open').forEach((node) => closeModal(node.id));
          closeFloatingPanels();
          if (state.adapterEditorOpen || state.editingAdapterId) {
            resetAdapterForm();
          } else if (state.settingsMode) {
            closeSettingsPage();
          }
        }
      });
      window.addEventListener('hashchange', () => {
        restoreRouteState().catch((error) => pushUiLog(`route restore failed: ${error.message}`, 'error'));
      });
      window.addEventListener('popstate', () => {
        restoreRouteState().catch((error) => pushUiLog(`route restore failed: ${error.message}`, 'error'));
      });
    };

    window.performSearch = performSearch;
    window.selectSearchResult = selectSearchResult;
    window.openExportDialog = openExportDialog;
    window.toggleTheme = toggleTheme;
    window.openLogDialog = openLogDialog;
    window.downloadExportPackage = downloadExportPackage;
    window.openOfflineAuditDialog = openOfflineAuditDialog;
    window.runOfflineAudit = runOfflineAudit;
    window.repairOfflineAudit = repairOfflineAudit;
    window.openImportDialog = openImportDialog;
    window.validateImportPackage = validateImportPackage;
    window.submitImportPackage = submitImportPackage;
    window.runManualBackup = runManualBackup;
    window.saveBackupSettings = saveBackupSettings;
    window.resetBackupSettings = resetBackupSettings;
    window.loginWithPassword = loginWithPassword;
    window.logoutAuth = logoutAuth;
    window.refreshAuthIdentity = refreshAuthIdentity;
    window.refreshCapturePolicies = refreshCapturePolicies;
    window.saveCapturePolicy = saveCapturePolicy;
    window.resetCapturePolicy = resetCapturePolicy;
    window.createAdminUser = createAdminUser;
    window.revokeAdminUser = revokeAdminUser;
    window.refreshAdminSessions = refreshAdminSessions;
    window.resetAdminUserPassword = resetAdminUserPassword;
    window.revokeAdminSession = revokeAdminSession;

    bindEvents();
    applyPalette();
    applyTheme();
    pushUiLog('控制台启动');
    renderAll();
    loadAdapters().catch((error) => {
      console.error(error);
      el('roomList').appendChild(Object.assign(document.createElement('div'), { className: 'empty', textContent: '控制台加载失败，请检查 API 或管理 Token。' }));
    });
