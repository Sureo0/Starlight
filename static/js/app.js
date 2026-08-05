(function () {
    'use strict';

    let conversations = [];
    let currentConvId = null;
    let currentUser = null;

    const $ = (s) => document.querySelector(s);
    const chatList = $('#chatList');
    const messagesEl = $('#messages');
    const welcomeEl = $('#welcome');
    const msgInput = $('#msgInput');
    const btnSend = $('#btnSend');
    const btnCancel = $('#btnCancel');
    const btnAttach = $('#btnAttach');
    const fileInput = $('#fileInput');
    const attachBar = $('#attachBar');
    const inputArea = $('#inputArea');
    const chatTitle = $('#chatTitle');

    // CSRF token from meta tag
    function getCsrfToken() {
        var meta = document.querySelector('meta[name="csrf-token"]');
        return meta ? meta.getAttribute('content') : '';
    }

    // ============================================================
    // Toast notifications
    // ============================================================
    function showToast(message, type) {
        type = type || 'error';
        var existing = document.querySelector('.toast');
        if (existing) existing.remove();

        var toast = document.createElement('div');
        toast.className = 'toast toast-' + type;
        toast.textContent = message;
        document.body.appendChild(toast);

        requestAnimationFrame(function () {
            toast.classList.add('toast-show');
        });

        setTimeout(function () {
            toast.classList.remove('toast-show');
            setTimeout(function () { toast.remove(); }, 300);
        }, 3500);
    }

    // ============================================================
    // Fetch wrapper with error handling
    // ============================================================
    async function apiFetch(url, options) {
        try {
            // Add CSRF token to state-changing requests
            options = options || {};
            if (options.method && options.method !== 'GET' && options.method !== 'HEAD') {
                options.headers = options.headers || {};
                options.headers['X-CSRF-Token'] = getCsrfToken();
            }

            var res = await fetch(url, options);

            if (res.status === 401) {
                currentUser = null;
                updateUserUI();
                showToast('Session expired. Please login again.');
                return null;
            }

            if (!res.ok) {
                var errData;
                try { errData = await res.json(); } catch (_) { errData = {}; }
                var errMsg = errData.error || ('Request failed (' + res.status + ')');
                showToast(errMsg);
                throw new Error(errMsg);
            }

            return res;
        } catch (e) {
            if (e.message && e.message.includes('Failed to fetch')) {
                showToast('Network error. Please check your connection.');
            }
            throw e;
        }
    }

    // ============================================================
    // User state
    // ============================================================
    async function checkLogin() {
        try {
            var res = await fetch('/api/user');
            if (res.ok) {
                var data = await res.json();
                currentUser = data.user;
            } else {
                currentUser = null;
            }
        } catch (e) {
            currentUser = null;
        }
        updateUserUI();
    }

    function updateUserUI() {
        var avatar = $('#userAvatar');
        var name = $('#userName');
        var header = $('#dropdownHeader');
        var btnLogin = $('#btnDropdownLogin');
        var btnChangePw = $('#btnDropdownChangePw');
        var btnLogout = $('#btnDropdownLogout');

        if (currentUser) {
            avatar.textContent = currentUser.charAt(0).toUpperCase();
            name.textContent = currentUser;
            header.textContent = currentUser;
            btnLogin.style.display = 'none';
            btnChangePw.style.display = '';
            btnLogout.style.display = '';
        } else {
            avatar.textContent = '?';
            name.textContent = 'Login';
            header.textContent = 'Not logged in';
            btnLogin.style.display = '';
            btnChangePw.style.display = 'none';
            btnLogout.style.display = 'none';
        }
    }

    // ============================================================
    // User menu (dropdown / login modal)
    // ============================================================
    function toggleUserMenu() {
        if (!currentUser) {
            openLoginModal();
        } else {
            var dd = $('#userDropdown');
            dd.classList.toggle('show');
        }
    }

    function closeDropdown() {
        $('#userDropdown').classList.remove('show');
    }

    function openLoginModal() {
        closeDropdown();
        $('#loginUsername').value = '';
        $('#loginPassword').value = '';
        $('#loginError').style.display = 'none';
        $('#loginModal').classList.add('active');
        setTimeout(function () { $('#loginUsername').focus(); }, 100);
    }

    function closeLoginModal() {
        $('#loginModal').classList.remove('active');
    }

    async function doLogin() {
        var username = $('#loginUsername').value.trim();
        var password = $('#loginPassword').value;
        var errEl = $('#loginError');

        if (!username || !password) {
            errEl.textContent = 'Please fill in all fields';
            errEl.style.display = 'block';
            return;
        }

        // Validate username format (alphanumeric + underscore, 3-20 chars)
        if (!/^[a-zA-Z0-9_]{3,20}$/.test(username)) {
            errEl.textContent = 'Username: 3-20 characters, letters, numbers, underscore only';
            errEl.style.display = 'block';
            return;
        }

        try {
            var res = await fetch('/login', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/x-www-form-urlencoded',
                    'X-CSRF-Token': getCsrfToken(),
                },
                body: 'username=' + encodeURIComponent(username) + '&password=' + encodeURIComponent(password),
                redirect: 'manual',
            });

            // Flask redirects to / on success (302)
            if (res.type === 'opaqueredirect' || res.status === 302 || res.redirected) {
                window.location.reload();
                return;
            }

            // If we got HTML back, it's the login page with error
            var text = await res.text();
            if (text.includes('incorrect') || text.includes('error')) {
                errEl.textContent = 'Username or password is incorrect';
                errEl.style.display = 'block';
            } else {
                window.location.reload();
            }
        } catch (e) {
            errEl.textContent = 'Login failed. Please try again.';
            errEl.style.display = 'block';
        }
    }

    // ============================================================
    // Change Password
    // ============================================================
    function openPwModal() {
        closeDropdown();
        $('#pwCurrent').value = '';
        $('#pwNew').value = '';
        $('#pwConfirm').value = '';
        $('#pwModal').classList.add('active');
        setTimeout(function () { $('#pwCurrent').focus(); }, 100);
    }

    function closePwModal() {
        $('#pwModal').classList.remove('active');
    }

    async function doChangePassword() {
        var curPw = $('#pwCurrent').value;
        var newPw = $('#pwNew').value;
        var confirmPw = $('#pwConfirm').value;

        if (!curPw || !newPw || !confirmPw) {
            showToast('Please fill in all fields');
            return;
        }
        if (newPw.length < 8) {
            showToast('Password must be at least 8 characters');
            return;
        }
        if (newPw !== confirmPw) {
            showToast('New passwords do not match');
            return;
        }

        try {
            var res = await apiFetch('/api/change-password', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    current_password: curPw,
                    new_password: newPw,
                }),
            });
            if (!res) return;
            var data = await res.json();
            if (data.ok) {
                showToast('Password changed successfully', 'success');
                closePwModal();
            } else {
                showToast(data.error || 'Failed to change password');
            }
        } catch (e) { /* toast already shown */ }
    }

    // ============================================================
    // Conversations
    // ============================================================
    async function loadConversations() {
        try {
            var res = await apiFetch('/api/conversations');
            if (!res) return;
            conversations = await res.json();
            renderChatList();
        } catch (e) { /* toast already shown */ }
    }

    function renderChatList() {
        chatList.innerHTML = conversations.map(c => `
            <div class="chat-item ${c.id === currentConvId ? 'active' : ''}" data-id="${c.id}">
                <span class="chat-item-title" title="${esc(c.title)}">${esc(c.title)}</span>
                <span class="chat-item-actions">
                    <button class="chat-item-ren" data-ren="${c.id}" title="重命名会话">&hellip;</button>
                    <button class="chat-item-del" data-del="${c.id}" title="删除会话">&times;</button>
                </span>
            </div>
        `).join('');

        chatList.querySelectorAll('.chat-item').forEach(el => {
            el.addEventListener('click', (e) => {
                if (e.target.classList.contains('chat-item-del')) return;
                if (e.target.classList.contains('chat-item-ren')) return;
                if (e.target.closest('.chat-item-rename-input')) return;
                loadConversation(el.dataset.id);
            });
        });
        chatList.querySelectorAll('.chat-item-del').forEach(btn => {
            btn.addEventListener('click', (e) => {
                e.stopPropagation();
                deleteConversation(btn.dataset.del);
            });
        });
        chatList.querySelectorAll('.chat-item-ren').forEach(btn => {
            btn.addEventListener('click', (e) => {
                e.stopPropagation();
                renameConversation(btn.dataset.ren);
            });
        });
    }

    async function renameConversation(id) {
        var item = chatList.querySelector('.chat-item[data-id="' + id + '"]');
        if (!item) return;
        var titleEl = item.querySelector('.chat-item-title');
        var conv = conversations.find(function (c) { return c.id === id; });
        var oldTitle = conv ? conv.title : (titleEl.textContent || '');

        // Replace the title span with an inline input
        var input = document.createElement('input');
        input.type = 'text';
        input.className = 'chat-item-rename-input';
        input.value = oldTitle;
        input.maxLength = 100;
        titleEl.replaceWith(input);
        input.focus();
        input.select();

        var done = false;
        async function commit() {
            if (done) return;
            done = true;
            var val = input.value.trim();
            if (!val || val === oldTitle) {
                cancel(); // no change — restore
                return;
            }
            try {
                var res = await apiFetch('/api/conversations/' + id + '/title', {
                    method: 'PUT',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ title: val }),
                });
                if (!res) { cancel(); return; }
                if (currentConvId === id) chatTitle.textContent = val;
                await loadConversations();
            } catch (e) {
                cancel();
            }
        }
        function cancel() {
            if (done) return;
            done = true;
            var span = document.createElement('span');
            span.className = 'chat-item-title';
            span.textContent = oldTitle;
            input.replaceWith(span);
        }

        input.addEventListener('keydown', function (e) {
            if (e.key === 'Enter') { e.preventDefault(); commit(); }
            else if (e.key === 'Escape') { e.preventDefault(); cancel(); }
        });
        input.addEventListener('blur', commit);
    }

    async function loadConversation(id) {
        try {
            var res = await apiFetch('/api/conversations/' + id);
            if (!res) return;
            var conv = await res.json();
            currentConvId = conv.id;
            chatTitle.textContent = conv.title;
            renderMessages(conv.messages);
            renderConvFiles(conv.files);
            renderChatList();
            closeSidebar();
        } catch (e) { /* toast already shown */ }
    }

    // --- Conversation files (uploaded in this conversation) ---
    function renderConvFiles(files) {
        var el = document.getElementById('convFiles');
        if (!el) return;
        if (!files || !files.length) {
            el.style.display = 'none';
            el.innerHTML = '';
            return;
        }
        el.style.display = 'block';
        el.innerHTML = '<div class="conv-files-title">📁 本会话上传的文件</div>' +
            '<div class="conv-files-list">' + files.map(function (f) {
                var thumb = (f.kind === 'image')
                    ? '<img class="attach-thumb" src="' + f.url + '" alt="">'
                    : '<span>📄</span>';
                return '<a class="conv-file-item" href="' + f.url + '" target="_blank" title="' + esc(f.name) + '">' +
                    thumb + '<span class="attach-name">' + esc(f.name) + '</span></a>';
            }).join('') + '</div>';
    }

    async function createConversation() {
        try {
            var res = await apiFetch('/api/conversations', { method: 'POST' });
            if (!res) return;
            var data = await res.json();
            currentConvId = data.id;
            chatTitle.textContent = 'New Chat';
            renderMessages([]);
            renderConvFiles([]);
            await loadConversations();
            closeSidebar();
        } catch (e) { /* toast already shown */ }
    }

    async function deleteConversation(id) {
        try {
            await apiFetch('/api/conversations/' + id, { method: 'DELETE' });
            if (currentConvId === id) {
                currentConvId = null;
                chatTitle.textContent = 'New Chat';
                renderMessages([]);
                renderConvFiles([]);
            }
            await loadConversations();
        } catch (e) { /* toast already shown */ }
    }

    // ============================================================
    // Messages
    // ============================================================
    function attachmentUrl(a) {
        if (a.url) return a.url;
        // Mounts point at live local directories — no single preview URL.
        if (a.kind === 'mount') return null;
        // History entries only carry file_id + name: build the preview URL
        // (files live in /api/uploads/<conv_id>/<file_id>.<ext> — the conv
        // the file was uploaded in, if known; else the legacy flat layout).
        if (a.file_id) {
            var fid = a.file_id;
            var ext = a.ext || (a.name ? a.name.split('.').pop() : '') || '';
            ext = String(ext).replace(/^\./, '');  // tolerate ".png" vs "png"
            var url = fid + (ext ? '.' + ext : '');
            if (a.conv_id) return '/api/uploads/' + a.conv_id + '/' + url;
            return '/api/uploads/' + url;
        }
        return null;
    }

    function attachmentsHtml(atts) {
        if (!atts || !atts.length) return '';
        // Mounted-folder references are NOT conversation files — never show
        // them in the message history (files the user uploaded are).
        var convAtts = atts.filter(function (a) { return a.kind !== 'mount'; });
        if (!convAtts.length) return '';
        return '<div class="msg-attachments">' + convAtts.map(function (a) {
            var url = attachmentUrl(a);
            var thumb = (a.kind === 'image' && url)
                ? '<img class="attach-thumb" src="' + url + '" alt="">'
                : '<span>📄</span>';
            return '<span class="attach-chip" title="' + esc(a.name || '') + '">' + thumb +
                '<span class="attach-name">' + esc(a.name || '') + '</span></span>';
        }).join('') + '</div>';
    }

    function renderMessages(messages) {
        if (!messages || messages.length === 0) {
            messagesEl.innerHTML = '';
            messagesEl.appendChild(welcomeEl);
            welcomeEl.style.display = 'block';
            return;
        }
        welcomeEl.style.display = 'none';
        messagesEl.innerHTML = messages.map(m => `
            <div class="msg ${m.role}">
                <div class="msg-avatar">${m.role === 'user' ? 'You' : 'AI'}</div>
                <div class="msg-body">${formatContent(m.content)}${attachmentsHtml(m.attachments)}</div>
            </div>
        `).join('');
        scrollToBottom();
    }

    function appendMessage(role, content, atts) {
        welcomeEl.style.display = 'none';
        var div = document.createElement('div');
        div.className = 'msg ' + role;
        div.innerHTML = '<div class="msg-avatar">' + (role === 'user' ? 'You' : 'AI') + '</div><div class="msg-body">' + formatContent(content) + '</div>';
        if (atts && atts.length) {
            var convAtts = atts.filter(function (a) { return a.kind !== 'mount'; });
            if (convAtts.length) {
                var body = div.querySelector('.msg-body');
                var wrap = document.createElement('div');
                wrap.className = 'msg-attachments';
                convAtts.forEach(function (a) {
                    var url = attachmentUrl(a);
                    var thumb = (a.kind === 'image' && url)
                        ? '<img class="attach-thumb" src="' + url + '" alt="">'
                        : '<span>📄</span>';
                    var chip = document.createElement('span');
                    chip.className = 'attach-chip';
                    chip.title = a.name || '';
                    chip.innerHTML = thumb + '<span class="attach-name">' + esc(a.name || '') + '</span>';
                    wrap.appendChild(chip);
                });
                body.appendChild(wrap);
            }
        }
        messagesEl.appendChild(div);
        scrollToBottom();
        return div;
    }

    function showTyping() {
        welcomeEl.style.display = 'none';
        var div = document.createElement('div');
        div.className = 'msg assistant';
        div.id = 'typingIndicator';
        div.innerHTML = '<div class="msg-avatar">AI</div><div class="typing"><span></span><span></span><span></span></div>';
        messagesEl.appendChild(div);
        scrollToBottom();
    }

    function hideTyping() {
        var el = document.getElementById('typingIndicator');
        if (el) el.remove();
    }

    // --- Cancellation state ---
    var currentRunId = null;      // run_id of the in-flight request
    var currentRunMode = 'chat';  // 'chat' (direct cancel) or 'task' (confirm first)
    var cancelConfirmed = false;  // user approved the cancel dialog

    var TASK_CUE_RE = /(写|创建|制作|分析|研究|整理|生成|开发|调查|搜索|检查|修复|部署|总结|对比|设计|实现)/;

    function isTaskMode(text) {
        return text.length >= 30 || TASK_CUE_RE.test(text);
    }

    function showCancelBtn() {
        btnSend.style.display = 'none';
        btnCancel.style.display = 'inline-flex';
        btnCancel.disabled = false;
        btnCancel.title = '停止生成';
    }
    function hideCancelBtn() {
        btnSend.style.display = 'inline-flex';
        btnCancel.style.display = 'none';
    }

    // --- Attachments (file/image upload) ---
    var pendingAttachments = [];  // {file_id, name, size, kind, ext, url}
    var activeVisionSupported = false;  // does the active model see images?

    function addAttachmentChip(att) {
        var chip = document.createElement('div');
        chip.className = 'attach-chip';
        var thumb = (att.kind === 'image' && att.url)
            ? '<img class="attach-thumb" src="' + att.url + '" alt="">'
            : (att.kind === 'mount'
                ? '<span title="挂载文件夹">🔗</span>'
                : '<span>📄</span>');
        var label = att.name;
        if (att.kind === 'mount') label += '（挂载）';
        chip.innerHTML = thumb +
            '<span class="attach-name" title="' + esc(label) + '">' + esc(label) + '</span>' +
            '<span class="attach-x" title="移除">×</span>';
        chip.querySelector('.attach-x').addEventListener('click', function () {
            pendingAttachments = pendingAttachments.filter(function (a) { return a.file_id !== att.file_id; });
            chip.remove();
            if (pendingAttachments.length === 0) attachBar.style.display = 'none';
        });
        attachBar.appendChild(chip);
        attachBar.style.display = 'flex';
    }

    function addFilesToQueue(fileList) {
        Array.prototype.forEach.call(fileList, function (file) {
            if (pendingAttachments.length >= 8) {
                showToast('最多同时上传 8 个文件');
                return;
            }
            var fd = new FormData();
            fd.append('file', file, file.name);
            // Conversation-scoped uploads: files land in data/uploads/<conv_id>/
            if (currentConvId) fd.append('conv_id', currentConvId);
            apiFetch('/api/upload', { method: 'POST', body: fd })
                .then(function (res) { return res ? res.json() : null; })
                .then(function (data) {
                    if (!data || !data.ok) { showToast('上传失败: ' + (data && data.error || 'unknown')); return; }
                    var att = { file_id: data.file_id, name: data.name, size: data.size, kind: data.kind, ext: data.ext, url: data.url, conv_id: data.conv_id || null };
                    pendingAttachments.push(att);
                    addAttachmentChip(att);
                })
                .catch(function () { showToast('上传失败: ' + file.name); });
        });
    }

    function wireDragDrop() {
        var dragDepth = 0;
        ['dragenter', 'dragover'].forEach(function (ev) {
            inputArea.addEventListener(ev, function (e) {
                e.preventDefault();
                inputArea.classList.add('dragover');
            });
        });
        inputArea.addEventListener('dragleave', function (e) {
            dragDepth--;
            if (dragDepth <= 0) { dragDepth = 0; inputArea.classList.remove('dragover'); }
        });
        inputArea.addEventListener('drop', function (e) {
            e.preventDefault();
            dragDepth = 0;
            inputArea.classList.remove('dragover');
            if (e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files.length) {
                addFilesToQueue(e.dataTransfer.files);
                showToast('已添加 ' + e.dataTransfer.files.length + ' 个文件');
            }
        });
    }

    async function requestCancel() {
        if (!currentRunId) return;
        try {
            if (currentRunMode === 'chat') {
                // Plain chat: cancel directly (no confirmation).
                btnCancel.disabled = true;
                await apiFetch('/api/cancel', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ run_id: currentRunId, mode: 'direct' }),
                });
                return;
            }
            // Task mode: ask the user first; only cancel after approval.
            if (!confirm('任务正在执行中，确定要取消吗？\n\n取消后当前进度将丢失。')) {
                await apiFetch('/api/cancel/' + encodeURIComponent(currentRunId) + '/deny', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({}),
                });
                return;
            }
            cancelConfirmed = true;
            btnCancel.disabled = true;
            btnCancel.title = '已请求取消，等待停止…';
            await apiFetch('/api/cancel', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ run_id: currentRunId, mode: 'confirm' }),
            });
            await apiFetch('/api/cancel/' + encodeURIComponent(currentRunId) + '/approve', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({}),
            });
        } catch (e) { /* silent */ }
    }

    async function sendMessage(text) {
        text = (text || msgInput.value).trim();
        if (!text && pendingAttachments.length === 0) return;

        var attsToSend = pendingAttachments.slice();
        var messageText = text;

        msgInput.value = '';
        msgInput.style.height = 'auto';
        btnSend.disabled = true;
        currentRunId = 'run-' + Date.now().toString(36) + '-' + Math.random().toString(36).slice(2, 8);
        currentRunMode = isTaskMode(text) ? 'task' : 'chat';
        cancelConfirmed = false;
        showCancelBtn();

        appendMessage('user', messageText, attsToSend);
        showTyping();

        // Attachments leave the composer immediately and ride along with the
        // message (they are rendered as chips inside the user bubble below).
        // Clearing here (not after the request) ensures they never stay
        // stuck in the input area even if the request fails.
        pendingAttachments = [];
        attachBar.innerHTML = '';
        attachBar.style.display = 'none';

        try {
            var res = await apiFetch('/api/chat', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    conversation_id: currentConvId,
                    message: messageText,
                    run_id: currentRunId,
                    attachments: attsToSend.map(function (a) { return { file_id: a.file_id, name: a.name, kind: a.kind || 'doc', conv_id: a.conv_id || null, ext: a.ext || '' }; }),
                }),
            });
            hideCancelBtn();
            if (!res) { hideTyping(); return; }
            var data = await res.json();
            hideTyping();

            if (data.error) {
                appendMessage('assistant', 'Error: ' + data.error);
            } else {
                currentConvId = data.conversation_id;
                chatTitle.textContent = data.title || chatTitle.textContent;
                if (data.cancelled) {
                    appendMessage('assistant', data.content || '（已取消）');
                    showToast('已停止生成');
                } else if (data.vision_blocked) {
                    appendMessage('assistant', data.content);
                    showToast('当前大模型不支持多模态', 'warning');
                } else {
                    appendMessage('assistant', data.content);
                }
                await loadConversations();
            }
        } catch (e) {
            hideCancelBtn();
            hideTyping();
            if (!e.message.includes('toast')) {
                appendMessage('assistant', 'Request failed: ' + e.message);
            }
        }

        btnSend.disabled = false;
        currentRunId = null;
        // Clear attachment queue
        pendingAttachments = [];
        attachBar.innerHTML = '';
        attachBar.style.display = 'none';
        msgInput.focus();
    }

    // ============================================================
    // Monitor
    // ============================================================
    var monitorInterval = null;

    function openMonitor() {
        $('#monitorModal').classList.add('active');
        loadStats();
        loadLogs();
        loadMcpStatus();
        wireMcpReload();
        // Auto-refresh every 5 seconds
        monitorInterval = setInterval(function () {
            loadStats();
            loadLogs();
            loadMcpStatus();
        }, 5000);
    }

    function closeMonitor() {
        $('#monitorModal').classList.remove('active');
        if (monitorInterval) {
            clearInterval(monitorInterval);
            monitorInterval = null;
        }
    }

    async function loadStats() {
        try {
            var res = await apiFetch('/api/stats');
            if (!res) return;
            var s = await res.json();

            // Application stats
            $('#statUptime').textContent = s.uptime_human || '-';
            $('#statRequests').textContent = s.total_requests || 0;

            var errRate = s.total_requests > 0
                ? ((s.error_count / s.total_requests) * 100).toFixed(1) + '%'
                : '0%';
            $('#statErrors').textContent = errRate;
            $('#statErrors').style.color = s.error_count > 0 ? '#dc2626' : '';

            $('#statLlmCalls').textContent = s.llm_calls || 0;
            $('#statTokens').textContent = s.llm_total_tokens
                ? s.llm_total_tokens.toLocaleString()
                : '0';
            $('#statLatency').textContent = s.llm_avg_duration_ms
                ? s.llm_avg_duration_ms + 'ms'
                : '-';
            $('#statConvs').textContent = s.conversation_count || 0;
            $('#statUsers').textContent = s.user_count || 0;

            // System resources
            if (s.system && !s.system.error) {
                var sys = s.system;
                $('#resCpu').textContent = sys.cpu_percent + '% (avg ' + sys.cpu_avg_1m + '%)';
                $('#resCpuBar').style.width = Math.min(sys.cpu_percent, 100) + '%';
                $('#resCpuBar').className = 'resource-fill' + (sys.cpu_percent > 90 ? ' danger' : sys.cpu_percent > 70 ? ' warning' : '');

                $('#resMem').textContent = sys.memory_percent + '% (' + sys.memory_used_mb + '/' + sys.memory_total_mb + ' MB)';
                $('#resMemBar').style.width = Math.min(sys.memory_percent, 100) + '%';
                $('#resMemBar').className = 'resource-fill' + (sys.memory_percent > 90 ? ' danger' : sys.memory_percent > 70 ? ' warning' : '');

                $('#resDisk').textContent = sys.disk_percent + '% (' + sys.disk_free_gb + ' GB free)';
                $('#resDiskBar').style.width = Math.min(sys.disk_percent, 100) + '%';
                $('#resDiskBar').className = 'resource-fill' + (sys.disk_percent > 90 ? ' danger' : sys.disk_percent > 80 ? ' warning' : '');
            }

            // Alert status
            if (s.alerts) {
                var alertHtml = '';
                var rules = s.alerts;
                if (rules.enabled !== undefined) {
                    alertHtml += '<div class="alert-badge ' + (rules.enabled ? 'on' : 'off') + '">'
                        + (rules.enabled ? 'Alerts ON' : 'Alerts OFF') + '</div>';
                }
                $('#alertRules').innerHTML = alertHtml || 'No alert data';

                // Recent alerts
                var recentHtml = '';
                if (s.alerts.recent && s.alerts.recent.length > 0) {
                    s.alerts.recent.forEach(function (a) {
                        recentHtml += '<div class="alert-item alert-' + esc(a.severity) + '">'
                            + '<span class="alert-time">' + esc(a.timestamp.substring(11, 19)) + '</span> '
                            + '<span class="alert-sev">[' + esc(a.severity.toUpperCase()) + ']</span> '
                            + '<span class="alert-msg">' + esc(a.message) + '</span>'
                            + '</div>';
                    });
                } else {
                    recentHtml = '<div class="alert-none">No recent alerts</div>';
                }
                $('#alertHistory').innerHTML = recentHtml;
            }

            // Model stats
            var modelHtml = '';
            if (s.llm_calls_by_model) {
                for (var model in s.llm_calls_by_model) {
                    modelHtml += '<div class="stat-row"><span>' + esc(model) + '</span><span>' + s.llm_calls_by_model[model] + '</span></div>';
                }
            }
            $('#modelStats').innerHTML = modelHtml || '<div class="stat-row">No data</div>';

            // Path stats
            var pathHtml = '';
            if (s.top_paths) {
                for (var path in s.top_paths) {
                    pathHtml += '<div class="stat-row"><span>' + esc(path) + '</span><span>' + s.top_paths[path] + '</span></div>';
                }
            }
            $('#pathStats').innerHTML = pathHtml || '<div class="stat-row">No data</div>';

            // Log files
            var filesHtml = '';
            if (s.log_files) {
                s.log_files.forEach(function (f) {
                    filesHtml += '<div class="file-row"><span>' + esc(f.name) + '</span><span>' + f.size_kb + ' KB</span></div>';
                });
            }
            $('#logFiles').innerHTML = filesHtml || '<div class="file-row">No log files</div>';
        } catch (e) { /* toast already shown */ }
    }

    async function loadLogs() {
        try {
            var res = await apiFetch('/api/logs');
            if (!res) return;
            var data = await res.json();
            var viewer = $('#logViewer');
            if (data.logs && data.logs.length > 0) {
                viewer.textContent = data.logs.join('\n');
                viewer.scrollTop = viewer.scrollHeight;
            } else {
                viewer.textContent = 'No log entries yet.';
            }
        } catch (e) { /* toast already shown */ }
    }

    // ============================================================
    // MCP servers
    // ============================================================
    async function loadMcpStatus() {
        var el = $('#mcpStatus');
        try {
            var res = await apiFetch('/api/mcp/servers');
            if (!res) return;
            var data = await res.json();
            if (!data.enabled || !data.servers.length) {
                el.innerHTML = '<div class="mcp-empty">未配置 MCP server。可在 data/config.yaml 的 mcp_servers 段添加。</div>';
                return;
            }
            el.innerHTML = '';
            data.servers.forEach(function (s) {
                var stateCls = s.state === 'connected' ? 'mcp-ok' : (s.state === 'error' ? 'mcp-err' : 'mcp-wait');
                var card = document.createElement('div');
                card.className = 'mcp-server-card';
                card.innerHTML =
                    '<div class="mcp-server-head">' +
                    '<span class="mcp-dot ' + stateCls + '"></span>' +
                    '<b>' + esc(s.name) + '</b>' +
                    '<span class="mcp-state">' + esc(s.state) + '</span>' +
                    '<span class="mcp-transport">' + esc(s.transport) + '</span>' +
                    '</div>' +
                    '<div class="mcp-server-meta">' +
                    '工具: ' + s.tools + ' | 权限: ' + esc(s.permission) +
                    (s.last_error ? ' | <span class="err-text">' + esc(s.last_error) + '</span>' : '') +
                    '</div>' +
                    (s.tool_names && s.tool_names.length
                        ? '<div class="mcp-tools">' + s.tool_names.map(function (n) {
                            return '<span class="mcp-tool-chip">' + esc(n) + '</span>';
                        }).join('') + '</div>'
                        : '');
                el.appendChild(card);
            });
        } catch (e) {
            el.innerHTML = '<div class="mcp-empty">加载失败: ' + esc(e.message) + '</div>';
        }
    }

    function wireMcpReload() {
        var btn = $('#btnMcpReload');
        if (!btn) return;
        btn.addEventListener('click', function () {
            btn.disabled = true;
            btn.textContent = 'Reloading...';
            apiFetch('/api/mcp/reload', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ confirmed: true })
            }).then(function (res) {
                return res.json();
            }).then(function (data) {
                btn.disabled = false;
                btn.textContent = 'Reload';
                if (data && data.error) {
                    alert('MCP reload 失败: ' + data.error);
                } else {
                    loadMcpStatus();
                }
            }).catch(function (e) {
                btn.disabled = false;
                btn.textContent = 'Reload';
                alert('MCP reload 失败: ' + e.message);
            });
        });
    }

    // ============================================================
    // Long-term memory
    // ============================================================
    var memoryState = { query: '', type: '' };

    function openMemory() {
        $('#memoryModal').classList.add('active');
        $('#memSearch').value = '';
        $('#memTypeFilter').value = '';
        memoryState = { query: '', type: '' };
        loadMemories();
    }

    function closeMemory() {
        $('#memoryModal').classList.remove('active');
    }

    async function loadMemories() {
        try {
            var res;
            if (memoryState.query) {
                // Keyword search endpoint
                var searchUrl = '/api/agent/memories/search?q=' + encodeURIComponent(memoryState.query)
                    + '&limit=100';
                if (memoryState.type) searchUrl += '&memory_type=' + encodeURIComponent(memoryState.type);
                res = await apiFetch(searchUrl);
            } else {
                // List endpoint
                var listUrl = '/api/agent/memories?limit=100';
                if (memoryState.type) listUrl += '&memory_type=' + encodeURIComponent(memoryState.type);
                res = await apiFetch(listUrl);
            }
            if (!res) return;
            var data = await res.json();
            var mems = data.memories || data.results || [];
            renderMemories(mems);
            $('#memCount').textContent = mems.length + ' memories';
        } catch (e) { /* toast already shown */ }
    }

    function renderMemories(mems) {
        var list = $('#memoryList');
        if (!mems || mems.length === 0) {
            list.innerHTML = '<div class="memory-empty">No memories found.</div>';
            return;
        }

        list.innerHTML = mems.map(function (m) {
            var imp = '';
            for (var i = 0; i < (m.importance || 0); i++) imp += '★';
            var date = (m.updated_at || m.created_at || '').substring(0, 10);
            return `
                <div class="memory-item" data-id="${m.id}">
                    <span class="memory-type-badge ${esc(m.memory_type)}">${esc(m.memory_type)}</span>
                    <div class="memory-content">
                        <div>${esc(m.content)}</div>
                        <div class="memory-meta">
                            <span class="memory-importance" title="Importance">${imp || '-'}</span>
                            <span>${date}</span>
                        </div>
                    </div>
                    <div class="memory-actions">
                        <button class="mem-del" title="Delete" data-id="${m.id}">&times;</button>
                    </div>
                </div>
            `;
        }).join('');

        list.querySelectorAll('.mem-del').forEach(function (btn) {
            btn.addEventListener('click', function () { deleteMemory(btn.dataset.id); });
        });
    }

    async function deleteMemory(id) {
        try {
            await apiFetch('/api/agent/memories/' + id, { method: 'DELETE' });
            showToast('Memory deleted', 'success');
            loadMemories();
        } catch (e) { /* toast already shown */ }
    }

    async function addMemory() {
        var content = $('#memNewContent').value.trim();
        if (!content) {
            showToast('Please enter memory content');
            return;
        }
        try {
            var res = await apiFetch('/api/agent/memories', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    content: content,
                    memory_type: $('#memNewType').value,
                    importance: parseInt($('#memNewImportance').value),
                }),
            });
            if (!res) return;
            var data = await res.json();
            if (data.ok) {
                showToast('Memory saved', 'success');
                $('#memNewContent').value = '';
                loadMemories();
            } else {
                showToast(data.duplicate_of ? 'Memory already exists' : 'Failed to save memory');
            }
        } catch (e) { /* toast already shown */ }
    }

    // ============================================================
    // Settings
    // ============================================================
    // Common LLM provider presets (fill name/api_base/model placeholders;
    // user can still edit everything after picking one)
    var LLM_PRESETS = [
        { name: 'DeepSeek', api_base: 'https://api.deepseek.com/v1', model: 'deepseek-chat' },
        { name: 'OpenAI', api_base: 'https://api.openai.com/v1', model: 'gpt-4o' },
        { name: '通义千问 (Qwen)', api_base: 'https://dashscope.aliyuncs.com/compatible-mode/v1', model: 'qwen-plus' },
        { name: '豆包 (Doubao)', api_base: 'https://ark.cn-beijing.volces.com/api/v3', model: 'doubao-1-5-pro-32k-250115' },
        { name: 'Kimi (月之暗面)', api_base: 'https://api.moonshot.cn/v1', model: 'kimi-k2-0711-preview' },
        { name: '智谱 (GLM)', api_base: 'https://open.bigmodel.cn/api/paas/v4', model: 'glm-4-plus' },
        { name: '腾讯混元 (Hunyuan)', api_base: 'https://api.hunyuan.cloud.tencent.com/v1', model: 'hunyuan-turbos-latest' },
        { name: '文心一言 (ERNIE)', api_base: 'https://qianfan.baidubce.com/v2', model: 'ernie-4.0-8k' },
        { name: 'Google Gemini', api_base: 'https://generativelanguage.googleapis.com/v1beta/openai', model: 'gemini-2.0-flash' },
        { name: 'Anthropic Claude', api_base: 'https://api.anthropic.com/v1', model: 'claude-sonnet-4-5' },
        { name: 'Ollama (本地)', api_base: 'http://localhost:11434/v1', model: 'llama3.1' },
        { name: 'Xiaomi MIMO', api_base: 'https://api.xiaomimimo.com/v1', model: 'mimo-v2.5-pro' },
    ];

    function fillPresetDropdown(activeName) {
        var sel = $('#cfgPreset');
        sel.innerHTML = '<option value="">自定义 / 不适用预设</option>';
        LLM_PRESETS.forEach(function (p) {
            var opt = document.createElement('option');
            opt.value = p.name;
            opt.textContent = p.name;
            sel.appendChild(opt);
        });
        // Try to match the current backend to a preset so the dropdown and
        // the form agree when the settings modal opens. Match by name first
        // (exact), then fall back to api_base (handles renamed backends).
        var matched = null;
        LLM_PRESETS.forEach(function (p) {
            if (p.name === activeName) matched = p.name;
        });
        if (!matched && activeName) {
            // Pull the active backend's api_base from the config via the
            // form fields (set by fillBackendForm before this is called).
            var base = $('#cfgApiBase').value;
            if (base) {
                LLM_PRESETS.forEach(function (p) {
                    if (p.api_base === base) matched = p.name;
                });
            }
        }
        sel.value = matched || '';
    }

    function applyPreset(name) {
        // "自定义" (empty value) clears the form for fresh manual entry
        if (!name) {
            $('#cfgName').value = '';
            $('#cfgApiBase').value = '';
            $('#cfgModel').value = '';
            $('#cfgApiKey').value = '';
            $('#cfgApiKey').placeholder = 'sk-...';
            return;
        }
        var preset = null;
        LLM_PRESETS.forEach(function (p) { if (p.name === name) preset = p; });
        if (!preset) return;
        $('#cfgName').value = preset.name;
        $('#cfgApiBase').value = preset.api_base;
        $('#cfgModel').value = preset.model;
        $('#cfgApiKey').value = '';
        $('#cfgApiKey').placeholder = 'sk-...';
    }

    async function loadSettings() {
        try {
            var res = await apiFetch('/api/config');
            if (!res) return;
            var cfg = await res.json();
            var backends = (cfg.llms && cfg.llms.backends) || [];
            var activeName = cfg.active_backend || (backends[0] ? backends[0].name : '');
            // Remember whether the active model supports vision
            var activeB = null;
            backends.forEach(function (b) { if (b.name === activeName) activeB = b; });
            activeB = activeB || backends[0] || {};
            activeVisionSupported = !!activeB.supports_vision;
            // Fill the form FIRST so fillPresetDropdown can match the active
            // backend's api_base (read from the form) against presets.
            fillBackendForm(cfg, activeName);
            fillPresetDropdown(activeName);
        } catch (e) { /* toast already shown */ }
    }

    function fillBackendForm(cfg, activeName) {
        var backends = (cfg.llms && cfg.llms.backends) || [];
        var backend = null;
        backends.forEach(function (b) { if (b.name === activeName) backend = b; });
        backend = backend || backends[0] || {};
        $('#cfgName').value = backend.name || '';
        $('#cfgApiBase').value = backend.api_base || '';
        $('#cfgApiKey').value = '';
        // Env-var backed keys show the ${ENV} reference (not the real key)
        $('#cfgApiKey').placeholder = backend.api_key_env
            ? '使用环境变量 ' + backend.api_key_env + '（留空保持）'
            : (backend.api_key_masked || 'sk-...');
        $('#cfgModel').value = backend.model || '';
        $('#cfgSystemPrompt').value = cfg.system_prompt || '';
        $('#cfgTemperature').value = cfg.temperature != null ? cfg.temperature : 0.7;
        $('#cfgMaxTokens').value = cfg.max_tokens != null ? cfg.max_tokens : 2048;
    }

    async function saveSettings() {
        var apiKey = $('#cfgApiKey').value;
        var backendName = $('#cfgName').value || 'Default';
        var backendData = {
            name: backendName,
            api_base: $('#cfgApiBase').value,
            model: $('#cfgModel').value,
            enabled: true,
        };
        if (apiKey) backendData.api_key = apiKey;

        try {
            await apiFetch('/api/config/backend', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(backendData),
            });
            await apiFetch('/api/config', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    system_prompt: $('#cfgSystemPrompt').value,
                    temperature: parseFloat($('#cfgTemperature').value),
                    max_tokens: parseInt($('#cfgMaxTokens').value),
                    // The backend edited in the form becomes the active one.
                    active_backend: backendName,
                }),
            });
            showToast('Settings saved', 'success');
            closeSettings();
        } catch (e) { /* toast already shown */ }
    }

    // ============================================================
    // Helpers
    // ============================================================
    function formatContent(text) {
        return text
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/```([\s\S]*?)```/g, '<pre><code>$1</code></pre>')
            .replace(/`([^`]+)`/g, '<code>$1</code>')
            .replace(/\n/g, '<br>');
    }

    function esc(s) { var d = document.createElement('div'); d.textContent = s; return d.innerHTML; }
    function scrollToBottom() { messagesEl.scrollTop = messagesEl.scrollHeight; }
    function autoResize(el) { el.style.height = 'auto'; el.style.height = Math.min(el.scrollHeight, 140) + 'px'; }
    function closeSidebar() { $('#sidebar').classList.remove('open'); }
    function openSettings() { loadSettings(); $('#settingsModal').classList.add('active'); }
    function closeSettings() { $('#settingsModal').classList.remove('active'); }

    // ============================================================
    // Event listeners
    // ============================================================
    window.sendSuggestion = function (text) { sendMessage(text); };

    // Chat
    btnSend.addEventListener('click', function () { sendMessage(); });
    btnCancel.addEventListener('click', function () { requestCancel(); });
    btnAttach.addEventListener('click', function () { fileInput.click(); });
    fileInput.addEventListener('change', function () {
        if (fileInput.files && fileInput.files.length) {
            addFilesToQueue(fileInput.files);
            fileInput.value = '';
        }
    });
    var btnFolder = document.getElementById('btnFolder');
    if (btnFolder) {
        btnFolder.addEventListener('click', function () { openMountModal(); });
    }
    wireDragDrop();
    msgInput.addEventListener('keydown', function (e) { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendMessage(); } });
    msgInput.addEventListener('input', function () { autoResize(msgInput); });
    $('#btnNewChat').addEventListener('click', createConversation);
    $('#btnToggleSidebar').addEventListener('click', function () { $('#sidebar').classList.toggle('open'); });

    // User menu
    $('#btnUser').addEventListener('click', function (e) {
        e.stopPropagation();
        toggleUserMenu();
    });
    document.addEventListener('click', closeDropdown);
    $('#userDropdown').addEventListener('click', function (e) { e.stopPropagation(); });

    // Login modal
    $('#btnDropdownLogin').addEventListener('click', openLoginModal);
    $('#btnCloseLogin').addEventListener('click', closeLoginModal);
    $('#btnCancelLogin').addEventListener('click', closeLoginModal);
    $('#btnDoLogin').addEventListener('click', doLogin);
    $('#loginModal').addEventListener('click', function (e) { if (e.target === e.currentTarget) closeLoginModal(); });
    $('#loginPassword').addEventListener('keydown', function (e) { if (e.key === 'Enter') doLogin(); });

    // Change password modal
    $('#btnDropdownChangePw').addEventListener('click', openPwModal);
    $('#btnClosePw').addEventListener('click', closePwModal);
    $('#btnCancelPw').addEventListener('click', closePwModal);
    $('#btnDoChangePw').addEventListener('click', doChangePassword);
    $('#pwModal').addEventListener('click', function (e) { if (e.target === e.currentTarget) closePwModal(); });
    $('#pwConfirm').addEventListener('keydown', function (e) { if (e.key === 'Enter') doChangePassword(); });

    // Logout
    $('#btnDropdownLogout').addEventListener('click', function () {
        window.location.href = '/logout';
    });

    // Settings (via gear menu)
    $('#btnGearSettings').addEventListener('click', function () { closeGearMenu(); openSettings(); });
    $('#btnCloseSettings').addEventListener('click', closeSettings);
    $('#btnCancelSettings').addEventListener('click', closeSettings);
    $('#btnSaveSettings').addEventListener('click', saveSettings);
    $('#settingsModal').addEventListener('click', function (e) { if (e.target === e.currentTarget) closeSettings(); });
    // Picking a preset fills the form (custom clears it; user can edit)
    $('#cfgPreset').addEventListener('change', function () {
        applyPreset(this.value);
    });

    // Monitor (via gear menu)
    $('#btnGearMonitor').addEventListener('click', function () { closeGearMenu(); openMonitor(); });
    $('#btnCloseMonitor').addEventListener('click', closeMonitor);
    $('#btnCloseMonitor2').addEventListener('click', closeMonitor);
    $('#monitorModal').addEventListener('click', function (e) { if (e.target === e.currentTarget) closeMonitor(); });
    $('#btnRunCheck').addEventListener('click', async function () {
        try {
            var res = await apiFetch('/api/alerts/check', { method: 'POST' });
            if (!res) return;
            var data = await res.json();
            if (data.triggered > 0) {
                showToast(data.triggered + ' alert(s) triggered', 'warning');
            } else {
                showToast('All checks passed', 'success');
            }
            loadStats();
        } catch (e) { /* toast already shown */ }
    });

    // Memory
    $('#btnMemory').addEventListener('click', openMemory);
    $('#btnCloseMemory').addEventListener('click', closeMemory);
    $('#btnCloseMemory2').addEventListener('click', closeMemory);
    $('#memoryModal').addEventListener('click', function (e) { if (e.target === e.currentTarget) closeMemory(); });
    $('#btnMemAdd').addEventListener('click', addMemory);
    $('#memNewContent').addEventListener('keydown', function (e) { if (e.key === 'Enter') addMemory(); });
    $('#btnMemSearch').addEventListener('click', function () {
        memoryState.query = $('#memSearch').value.trim();
        loadMemories();
    });
    $('#memSearch').addEventListener('keydown', function (e) {
        if (e.key === 'Enter') {
            memoryState.query = $('#memSearch').value.trim();
            loadMemories();
        }
    });
    $('#memTypeFilter').addEventListener('change', function () {
        memoryState.type = $('#memTypeFilter').value;
        loadMemories();
    });
    $('#btnMemReset').addEventListener('click', function () {
        $('#memSearch').value = '';
        $('#memTypeFilter').value = '';
        memoryState = { query: '', type: '' };
        loadMemories();
    });

    // ============================================================
    // Human-in-the-loop approval (approval cards)
    // ============================================================
    var approvalStack = $('#approvalStack');
    var approvalPollTimer = null;
    var knownApprovals = {};

    function pollApprovals() {
        if (!currentUser) return;
        apiFetch('/api/approvals')
            .then(function (res) { return res ? res.json() : null; })
            .then(function (data) {
                if (!data || !data.pending) return;
                data.pending.forEach(renderApprovalCard);
            })
            .catch(function () { /* silent — polling is best-effort */ });
    }

    function renderApprovalCard(req) {
        var card = document.getElementById('approval-' + req.id);
        if (card) {
            // Already rendered: update the countdown only
            updateApprovalExpiry(card, req);
            return;
        }
        knownApprovals[req.id] = true;

        card = document.createElement('div');
        card.className = 'approval-card';
        card.id = 'approval-' + req.id;

        var argsText = req.args ? JSON.stringify(req.args, null, 2) : '{}';
        card.innerHTML =
            '<div class="ap-title">⚠️ 需要确认 <span class="ap-tool">' + esc(req.tool) + '</span></div>' +
            '<div class="ap-args">' + esc(argsText) + '</div>' +
            '<div class="ap-actions">' +
            '  <button class="ap-btn ap-btn-approve" data-action="approve">批准</button>' +
            '  <button class="ap-btn ap-btn-reject" data-action="reject">拒绝</button>' +
            '</div>' +
            '<div class="ap-expires">等待确认…</div>';
        approvalStack.appendChild(card);

        card.querySelector('.ap-btn-approve').addEventListener('click', function () {
            decideApproval(req.id, 'approve', card);
        });
        card.querySelector('.ap-btn-reject').addEventListener('click', function () {
            decideApproval(req.id, 'reject', card);
        });
        updateApprovalExpiry(card, req);
        scrollToBottom();
    }

    function updateApprovalExpiry(card, req) {
        var exp = card.querySelector('.ap-expires');
        if (!exp) return;
        var left = req.expires_at ? (new Date(req.expires_at).getTime() - Date.now()) : 0;
        if (left <= 0) {
            exp.textContent = '已超时';
            card.classList.add('ap-done');
        } else {
            exp.textContent = '等待确认… ' + Math.ceil(left / 1000) + 's 后超时';
        }
    }

    function decideApproval(reqId, action, card) {
        apiFetch('/api/approvals/' + reqId + '/' + action, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(action === 'reject' ? { reason: '用户拒绝' } : {}),
        })
            .then(function (res) { return res ? res.json() : null; })
            .then(function (data) {
                if (data && data.ok) {
                    card.classList.add('ap-done');
                    card.querySelector('.ap-actions').style.display = 'none';
                    card.querySelector('.ap-expires').textContent =
                        action === 'approve'
                            ? '✓ 已批准，本次任务内不再询问'
                            : '✗ 已拒绝，本次任务内不再询问';
                    // The paused agent resumes on its own; re-render after
                    // it continues (the request leaves 'pending').
                    setTimeout(function () {
                        var c = document.getElementById('approval-' + reqId);
                        if (c) c.remove();
                    }, 6000);
                } else if (data && data.message) {
                    showToast(data.message);
                }
            })
            .catch(function (e) {
                showToast('审批操作失败: ' + e.message);
            });
    }

    function startApprovalPolling() {
        if (approvalPollTimer) return;
        approvalPollTimer = setInterval(pollApprovals, 3000);
        pollApprovals();
    }

    // ============================================================
    // Mounted folders (挂载目录)
    // ============================================================
    var mountModal = document.getElementById('mountModal');
    var mountPathInput = document.getElementById('mountPath');
    var mountListEl = document.getElementById('mountList');

    function openMountModal() {
        mountModal.classList.add('active');
        loadMountList();
        setTimeout(function () { mountPathInput.focus(); }, 100);
    }
    function closeMountModal() { mountModal.classList.remove('active'); }

    async function loadMountList() {
        if (!mountListEl) return;
        try {
            var res = await apiFetch('/api/mounts');
            if (!res) return;
            var data = await res.json();
            var mounts = data.mounts || [];
            mountListEl.innerHTML = '';
            if (!mounts.length) {
                mountListEl.innerHTML = '<div class="mount-empty" style="color:#94a3b8;font-size:0.85rem;">尚未挂载任何文件夹</div>';
                return;
            }
            mounts.forEach(function (m) {
                var row = document.createElement('div');
                row.className = 'mount-row';
                row.style.cssText = 'display:flex;align-items:center;gap:8px;padding:6px 8px;border:1px solid var(--border);border-radius:8px;margin-bottom:6px;';
                var policyLabel = m.policy === 'allow' ? '同任务内允许' : '总是询问';
                row.innerHTML =
                    '<span style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">' +
                    '📁 <b>' + esc(m.name) + '</b> <span style="color:#94a3b8;font-size:0.8rem;">' + esc(m.path) + '</span>' +
                    '<span style="margin-left:6px;font-size:0.7rem;padding:1px 6px;border-radius:6px;background:' + (m.policy === 'allow' ? 'rgba(34,197,94,0.12);color:#16a34a;' : 'rgba(99,102,241,0.12);color:#6366f1;') + '">' + policyLabel + '</span></span>' +
                    '<button class="btn-sm" style="flex-shrink:0;" data-mid="' + esc(m.id) + '">使用</button>' +
                    '<button class="btn-sm" style="flex-shrink:0;color:#dc2626;" data-mid="' + esc(m.id) + '">卸载</button>';
                row.querySelectorAll('button').forEach(function (btn) {
                    btn.addEventListener('click', function () {
                        var mid = btn.getAttribute('data-mid');
                        if (btn.textContent.trim() === '卸载') {
                            unmountFolder(mid);
                        } else {
                            useMount(mid);
                        }
                    });
                });
                mountListEl.appendChild(row);
            });
        } catch (e) { /* silent */ }
    }

    function unmountFolder(mid) {
        apiFetch('/api/mounts/' + mid, { method: 'DELETE' })
            .then(function (res) { return res ? res.json() : null; })
            .then(function (data) {
                if (data && data.ok) {
                    showToast('已卸载');
                    loadMountList();
                } else {
                    showToast(data && data.error || '卸载失败');
                }
            })
            .catch(function () { showToast('卸载失败'); });
    }

    function useMount(mid) {
        var mount = null;
        apiFetch('/api/mounts')
            .then(function (res) { return res ? res.json() : null; })
            .then(function (data) {
                if (!data) return;
                mount = (data.mounts || []).find(function (m) { return m.id === mid; });
                if (!mount) { showToast('挂载不存在'); return; }
                if (pendingAttachments.length >= 8) { showToast('最多同时上传 8 个附件'); return; }
                var att = { file_id: mount.id, name: mount.name, size: 0, kind: 'mount', ext: '', url: null };
                pendingAttachments.push(att);
                addAttachmentChip(att);
                closeMountModal();
            })
            .catch(function () { showToast('加载挂载列表失败'); });
    }

    // Mount confirm modal: after the user types a path and clicks 挂载, ask
    // how the agent may access the folder. 取消 → abort (no mount).
    // 总是询问 → every access asks. 允许 → first access per run asks, then
    // approved accesses within the run proceed silently.
    var mountConfirmModal = document.getElementById('mountConfirmModal');
    var mountConfirmName = document.getElementById('mountConfirmName');
    var mountConfirmPath = document.getElementById('mountConfirmPath');
    var pendingMountPath = null;

    function openMountConfirm(path) {
        pendingMountPath = path;
        mountConfirmName.textContent = path.split(/[\\\/]/).pop() || path;
        mountConfirmPath.textContent = path;
        mountConfirmModal.classList.add('active');
    }
    function closeMountConfirm() {
        pendingMountPath = null;
        mountConfirmModal.classList.remove('active');
    }

    function doMountWithPolicy(policy) {
        if (!pendingMountPath) return;
        apiFetch('/api/mounts', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ path: pendingMountPath, policy: policy }),
        })
            .then(function (res) { return res ? res.json() : null; })
            .then(function (data) {
                closeMountConfirm();
                if (data && data.ok) {
                    showToast('已挂载: ' + data.mount.name, 'success');
                    mountPathInput.value = '';
                    loadMountList();
                } else {
                    showToast(data && data.error || '挂载失败');
                }
            })
            .catch(function () { showToast('挂载失败'); });
    }

    document.getElementById('btnDoMount').addEventListener('click', function () {
        var path = mountPathInput.value.trim();
        if (!path) { showToast('请输入文件夹路径'); return; }
        openMountConfirm(path);
    });
    // 取消 → abort the mount entirely
    document.getElementById('btnMountConfirmCancel').addEventListener('click', function () {
        closeMountConfirm();
        showToast('已取消挂载');
    });
    // 总是询问 → every access asks for confirmation
    document.getElementById('btnMountConfirmAlwaysAsk').addEventListener('click', function () {
        doMountWithPolicy('always_ask');
    });
    // 允许 → first access per run asks, then approved accesses proceed
    document.getElementById('btnMountConfirmAllow').addEventListener('click', function () {
        doMountWithPolicy('allow');
    });
    document.getElementById('btnCloseMountConfirm').addEventListener('click', closeMountConfirm);
    if (mountConfirmModal) {
        mountConfirmModal.addEventListener('click', function (e) { if (e.target === e.currentTarget) closeMountConfirm(); });
    }
    document.getElementById('btnCloseMount').addEventListener('click', closeMountModal);
    document.getElementById('btnCancelMount').addEventListener('click', closeMountModal);
    if (mountModal) {
        mountModal.addEventListener('click', function (e) { if (e.target === e.currentTarget) closeMountModal(); });
    }
    mountPathInput.addEventListener('keydown', function (e) { if (e.key === 'Enter') document.getElementById('btnDoMount').click(); });

    // ============================================================
    // Gear menu (bottom-right) + Traces panel
    // ============================================================
    var gearFab = document.getElementById('gearFab');
    var gearMenu = document.getElementById('gearMenu');

    function closeGearMenu() { if (gearMenu) gearMenu.classList.remove('show'); }
    function toggleGearMenu(e) {
        if (e) e.stopPropagation();
        if (gearMenu) gearMenu.classList.toggle('show');
    }
    if (gearFab) gearFab.addEventListener('click', toggleGearMenu);
    document.addEventListener('click', function (e) {
        if (gearMenu && !gearMenu.contains(e.target) && gearFab && !gearFab.contains(e.target)) {
            closeGearMenu();
        }
    });

    // Traces panel: close button hides it; the gear menu can re-open it.
    // The gear FAB position follows the panel: when the panel is hidden the
    // FAB (and its menu) move to the right edge so they never overlap the
    // message send button in the middle column.
    var btnCloseTraces = document.getElementById('btnCloseTraces');
    var tracesPanel = document.getElementById('tracesPanel');

    function updateGearPosition(panelHidden) {
        if (panelHidden) {
            if (gearFab) gearFab.style.right = '20px';
            if (gearMenu) gearMenu.style.right = '20px';
        } else {
            if (gearFab) gearFab.style.right = '';
            if (gearMenu) gearMenu.style.right = '';
        }
    }

    if (btnCloseTraces) {
        btnCloseTraces.addEventListener('click', function () {
            if (tracesPanel) {
                tracesPanel.classList.add('hidden');
                updateGearPosition(true);
            }
        });
    }
    var btnGearTraces = document.getElementById('btnGearTraces');
    if (btnGearTraces) {
        btnGearTraces.addEventListener('click', function () {
            closeGearMenu();
            if (tracesPanel) {
                tracesPanel.classList.remove('hidden');
                updateGearPosition(false);
            }
        });
    }

    // ============================================================
    // Mouse-tracking backdrop (后背景.png)
    // Symmetric fade: opacity 0% at the vertical center, growing linearly
    // to 100% at BOTH the top and bottom edges as the mouse moves away
    // from the center (up or down). The layer is pointer-events:none +
    // z-index 0 so it never covers or blocks functional UI, and the
    // sidebar / traces panels are fully opaque white so the image never
    // shows there.
    // ============================================================
    var backdrop = document.getElementById('backdrop');
    if (backdrop) {
        var lastOpacity = -1;
        document.addEventListener('mousemove', function (e) {
            var h = window.innerHeight;
            if (!h) return;
            // Distance from the vertical center, normalized to 0..1
            // (1 = at the very top or bottom edge).
            var offset = Math.abs(e.clientY - h / 2) / (h / 2);
            var opacity = Math.min(1, offset);
            opacity = Math.round(opacity * 1000) / 1000;
            if (opacity !== lastOpacity) {
                lastOpacity = opacity;
                backdrop.style.opacity = opacity.toFixed(3);
            }
        });
    }

    // ============================================================
    // Chat section collapse (会话记录 展开/收起)
    // "Chat" label + ">" arrow form ONE clickable toggle: default expanded;
    // clicking anywhere on it (text or arrow) toggles the conversation list.
    // ============================================================
    var chatSection = document.getElementById('chatSection');
    var btnChatToggle = document.getElementById('btnChatToggle');
    if (chatSection && btnChatToggle) {
        btnChatToggle.addEventListener('click', function () {
            chatSection.classList.toggle('collapsed');
        });
    }

    // ============================================================
    // Init
    // ============================================================
    checkLogin();
    loadConversations();
    startApprovalPolling();
})();
