/* Agent run trace replay UI */
(function () {
    'use strict';

    var CSRF = (document.querySelector('meta[name="csrf-token"]') || {}).content || '';
    var currentFilter = 'all';
    var selectedTraceId = null;
    var allTraces = [];
    // Guards against out-of-order responses: if the user clicks trace A then
    // quickly trace B, only the LATEST request may render (a slow A response
    // arriving after B used to overwrite B's detail pane).
    var detailRequestSeq = 0;

    function api(path, options) {
        options = options || {};
        options.headers = Object.assign({
            'X-CSRF-Token': CSRF,
            'Content-Type': 'application/json'
        }, options.headers || {});
        return fetch(path, options).then(function (res) {
            if (!res.ok) {
                return res.json().then(function (d) {
                    throw new Error(d.error || res.statusText);
                });
            }
            return res.json();
        });
    }

    function esc(s) {
        return String(s == null ? '' : s)
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    }

    function fmtTime(ts) {
        if (!ts) return '';
        var d = new Date(ts * 1000);
        return d.toLocaleString('zh-CN', { hour12: false });
    }

    function fmtDur(sec) {
        if (sec == null) return '';
        return sec < 1 ? Math.round(sec * 1000) + 'ms' : sec.toFixed(1) + 's';
    }

    function highlightJson(obj) {
        var s = JSON.stringify(obj, null, 1);
        return esc(s).replace(/"([^"]+)"\s*:/g, '<span class="arg-key">"$1"</span>:');
    }

    function reasonLabel(reason) {
        var map = {
            text_response: '正常完成',
            no_valid_tool_calls: '无有效工具调用',
            tool_limit: '工具调用上限',
            iteration_limit: '迭代上限',
            timeout: '执行超时',
            loop_detected: '循环检测',
            failure_loop: '连续失败熔断',
            llm_error: 'LLM 错误',
            unexpected_type: '异常响应类型',
            validation_error: '消息验证失败',
            rate_limited: '频率限制',
            busy: '系统繁忙',
            error: '错误'
        };
        return map[reason] || reason;
    }

    // ============ List ============

    function loadList() {
        var query = 'limit=200';
        if (currentFilter === 'success') query += '&success=true';
        if (currentFilter === 'failed') query += '&success=false';
        api('/api/agent/traces?' + query).then(function (data) {
            allTraces = data.traces || [];
            renderList();
        }).catch(function (e) {
            document.getElementById('traceList').innerHTML =
                '<div class="trace-empty">加载失败: ' + esc(e.message) + '</div>';
        });
    }

    function renderList() {
        var el = document.getElementById('traceList');
        if (!allTraces.length) {
            el.innerHTML = '<div class="trace-empty">还没有运行记录。<br>在聊天页让 agent 执行一次任务后，<br>这里会出现完整的运行回放。</div>';
            return;
        }
        el.innerHTML = '';
        allTraces.forEach(function (t) {
            var item = document.createElement('div');
            item.className = 'trace-item' + (t.trace_id === selectedTraceId ? ' active' : '');
            item.setAttribute('data-trace-id', t.trace_id);
            var badge = t.success
                ? '<span class="badge badge-ok">成功</span>'
                : '<span class="badge badge-fail">失败</span>';
            item.innerHTML =
                '<div class="trace-item-title">' + esc(t.user_message || '(空消息)') + '</div>' +
                '<div class="trace-item-meta">' + badge +
                ' <span>' + esc(reasonLabel(t.finish_reason)) + '</span>' +
                ' <span>' + t.tool_calls_made + ' 次工具</span>' +
                ' <span>' + fmtDur(t.duration) + '</span>' +
                '</div>' +
                '<div class="trace-item-meta">' + fmtTime(t.started_at) +
                (t.model ? ' · ' + esc(t.model) : '') + '</div>';
            // NOTE: no per-item listener — see delegated listener below
            el.appendChild(item);
        });
    }

    // ============ Detail ============

    function selectTrace(id) {
        selectedTraceId = id;
        var seq = ++detailRequestSeq;
        renderList();
        // detailEmpty lives INSIDE detailScroll — the first render replaces
        // detailScroll's innerHTML and destroys it, so guard against null
        // (a missing element here used to throw and freeze the detail pane
        // on the first trace forever).
        var empty = document.getElementById('detailEmpty');
        if (empty) empty.style.display = 'none';
        document.getElementById('detailScroll').innerHTML = '<div class="trace-empty">加载中…</div>';
        api('/api/agent/traces/' + id).then(function (t) {
            if (seq !== detailRequestSeq) return; // stale response — a newer click won
            renderDetail(t);
        }).catch(function (e) {
            if (seq !== detailRequestSeq) return;
            document.getElementById('detailScroll').innerHTML =
                '<div class="trace-empty">加载失败: ' + esc(e.message) + '</div>';
        });
    }

    function renderDetail(t) {
        document.getElementById('detailTitle').textContent =
            (t.user_message || '(空消息)').slice(0, 60);
        document.getElementById('detailMeta').innerHTML =
            '<span>状态: <b>' + (t.success ? '成功' : '失败') + '</b> (' + esc(reasonLabel(t.finish_reason)) + ')' +
            (t.finish_detail ? ' — ' + esc(t.finish_detail) : '') + '</span>' +
            '<span>耗时: <b>' + fmtDur(t.duration) + '</b></span>' +
            '<span>工具调用: <b>' + t.tool_calls_made + '</b></span>' +
            '<span>迭代: <b>' + t.iterations + '</b></span>' +
            '<span>Tokens: <b>' + (t.total_tokens || 0) + '</b></span>' +
            (t.model ? '<span>模型: <b>' + esc(t.model) + '</b></span>' : '') +
            '<span>' + fmtTime(t.started_at) + '</span>' +
            '<button class="delete-btn" id="btnDeleteTrace">删除记录</button>';

        document.getElementById('btnDeleteTrace').addEventListener('click', function () {
            if (!confirm('确认删除这条运行记录？')) return;
            api('/api/agent/traces/' + t.trace_id, { method: 'DELETE' }).then(function () {
                selectedTraceId = null;
                document.getElementById('detailTitle').textContent = '选择一个运行记录查看回放';
                document.getElementById('detailMeta').innerHTML = '';
                document.getElementById('detailScroll').innerHTML =
                    '<div class="trace-empty" style="display:block">记录已删除。</div>';
                loadList();
            }).catch(function (e) { alert('删除失败: ' + e.message); });
        });

        var scroll = document.getElementById('detailScroll');
        scroll.innerHTML = '';

        // User message bubble
        var userBubble = document.createElement('div');
        userBubble.className = 'trace-user-msg';
        userBubble.textContent = t.user_message || '(空消息)';
        scroll.appendChild(userBubble);

        if (t.plan_generated) {
            scroll.appendChild(evElement({
                type: 'plan',
                detail: '目标: ' + t.plan_goal + ' · ' + t.plan_steps + ' 步'
            }, '执行计划'));
        }

        // Events
        (t.events || []).forEach(function (e) {
            var el = evElement(e, null);
            if (el) scroll.appendChild(el);
        });

        // Final answer
        if (t.content) {
            var final = document.createElement('div');
            final.className = 'trace-final';
            final.textContent = t.content;
            scroll.appendChild(final);
        }
        scroll.scrollTop = 0;
    }

    function evElement(e, overrideTitle) {
        var map = {
            llm_call: { title: 'LLM 调用', dot: 'ev-llm_call' },
            tool_call: { title: '工具调用', dot: 'ev-tool_call' },
            tool_result: { title: '工具结果', dot: 'ev-tool_result' },
            plan: { title: '计划', dot: 'ev-plan' },
            plan_progress: { title: '计划进度', dot: 'ev-plan_progress' },
            plan_review: { title: 'LLM 进度复核', dot: 'ev-plan_review' },
            memory_inject: { title: '记忆注入', dot: 'ev-memory_inject' },
            memory_extract: { title: '记忆抽取', dot: 'ev-memory_extract' },
            compression: { title: '上下文压缩', dot: 'ev-compression' },
            subagent: { title: '子代理', dot: 'ev-subagent' },
            security: { title: '安全拦截', dot: 'ev-security' },
            loop_guard: { title: '循环防护', dot: 'ev-loop_guard' },
            approval: { title: '人工确认', dot: 'ev-approval' },
            error: { title: '错误', dot: 'ev-error' },
            info: { title: '信息', dot: 'ev-info' }
        };
        var cfg = map[e.type];
        if (!cfg) return null;

        var wrap = document.createElement('div');
        wrap.className = 'event';

        var head = document.createElement('div');
        head.className = 'event-head';
        head.innerHTML =
            '<span class="ev-dot ' + cfg.dot + '"></span>' +
            '<span>' + esc(overrideTitle || cfg.title) + '</span>' +
            (e.tool ? ' <span class="tool-name">' + esc(e.tool) + '</span>' : '') +
            (e.iteration ? ' <span class="ev-msg">#迭代 ' + e.iteration + '</span>' : '') +
            '<span class="ev-time">' + fmtTime(e.ts) +
            (e.duration != null ? ' · ' + fmtDur(e.duration) : '') + '</span>';
        wrap.appendChild(head);

        var body = document.createElement('div');
        body.className = 'event-body';
        var html = '';

        if (e.detail) html += '<div class="ev-msg">' + esc(e.detail) + '</div>';

        if (e.type === 'llm_call') {
            // Response content (model reasoning / answer)
            if (e.response) {
                html += '<div class="ev-msg">回复:</div><pre>' + esc(e.response) + '</pre>';
            }
            // Tool calls requested by the model
            if (e.tool_calls && e.tool_calls.length) {
                html += '<div class="ev-msg">请求调用工具:</div>';
                e.tool_calls.forEach(function (tc) {
                    html += '<div class="tool-row">→ <span class="tool-name">' + esc(tc.name) +
                        '</span></div><pre>' + highlightJson(tc.args || {}) + '</pre>';
                });
            }
            // Token usage
            if (e.usage && (e.usage.total_tokens || e.usage.prompt_tokens)) {
                html += '<div class="ev-msg">用量: prompt ' + (e.usage.prompt_tokens || 0) +
                    ' / completion ' + (e.usage.completion_tokens || 0) +
                    ' / total ' + (e.usage.total_tokens || 0) +
                    (e.mode ? ' · 模式: ' + esc(e.mode) : '') + '</div>';
            }
            // Prompt (collapsible)
            if (e.messages && e.messages.length) {
                var promptHtml = e.messages.map(function (m) {
                    var line = '<b>[' + esc(m.role) + ']</b> ' + esc((m.content || '').slice(0, 1200));
                    if (m.tool_calls) {
                        line += ' ' + m.tool_calls.map(function (tc) {
                            return esc(tc.name);
                        }).join(', ');
                    }
                    return line;
                }).join('\n');
                html += '<div class="ev-msg">发送给模型的上下文 (点击展开/收起):</div>' +
                    '<pre data-fold>' + esc(promptHtml) + '</pre>';
            }
        } else if (e.type === 'tool_call') {
            html += '<pre>' + highlightJson(e.args || {}) + '</pre>';
        } else if (e.type === 'tool_result') {
            var r = e.result || {};
            var cls = r.success ? 'ok-text' : 'err-text';
            html += '<div class="ev-msg">' + (r.success ? '✓ 成功' : '✗ 失败') +
                (e.detail === 'cached' ? ' (命中缓存)' : '') +
                (r.error ? ' — ' + esc(String(r.error)) : '') + '</div>';
            if (e.retries && e.retries.length) {
                html += '<div class="ev-msg">重试 ' + e.retries.length + ' 次: ' +
                    esc(e.retries.map(function (x) { return '#' + x.attempt + ' ' + x.error; }).join(' | ')) + '</div>';
            }
            if (r.output != null) {
                var out = r.output;
                if (typeof out === 'string') out = out.slice(0, 2000);
                html += '<pre class="' + cls + '">' + esc(JSON.stringify(out, null, 1)) + '</pre>';
            }
        } else if (e.type === 'memory_inject' || e.type === 'memory_extract' || e.type === 'plan' ||
                   e.type === 'plan_progress' || e.type === 'plan_review' || e.type === 'compression') {
            if (e.content) html += '<pre>' + esc(e.content) + '</pre>';
        } else if (e.type === 'subagent') {
            // Sub-agent (delegate) summary card
            var sa = e.result || {};
            var saCls = sa.success ? 'ok-text' : 'err-text';
            html += '<div class="ev-msg">' + (sa.success ? '✓ 子代理完成' : '✗ 子代理失败') +
                (sa.mode ? ' · 模式: <b>' + esc(sa.mode) + '</b>' : '') +
                (sa.tool_calls_made != null ? ' · 工具调用: <b>' + sa.tool_calls_made + '</b>' : '') +
                (sa.error ? ' — <span class="err-text">' + esc(sa.error) + '</span>' : '') +
                '</div>';
            if (sa.trace_id) {
                html += '<div class="ev-msg">子代理追踪: <a href="/traces?trace=' + esc(sa.trace_id) +
                    '" class="trace-link">' + esc(sa.trace_id) + ' ↗</a></div>';
            }
            if (sa.content) {
                html += '<div class="ev-msg">子代理回答:</div><pre class="' + saCls + '">' +
                    esc(sa.content.slice(0, 2000)) + '</pre>';
            }
        } else if (e.type === 'error') {
            html += '<pre class="err-text">' + esc(e.error || '') + '</pre>';
        } else if (e.type === 'security' || e.type === 'loop_guard' || e.type === 'approval') {
            html += '<pre>' + esc(e.detail || '') + '</pre>';
        }

        body.innerHTML = html;
        wrap.appendChild(body);

        // Collapsible prompt blocks
        var fold = body.querySelector('pre[data-fold]');
        if (fold) {
            var promptPre = fold;
            promptPre.style.cursor = 'pointer';
            promptPre.style.maxHeight = '180px';
            promptPre.style.overflow = 'hidden';
            promptPre.title = '点击展开';
            promptPre.addEventListener('click', function () {
                if (promptPre.style.maxHeight === '180px') {
                    promptPre.style.maxHeight = 'none';
                    promptPre.style.overflow = 'visible';
                } else {
                    promptPre.style.maxHeight = '180px';
                    promptPre.style.overflow = 'hidden';
                }
            });
        }
        return wrap;
    }

    // ============ Init ============

    document.querySelectorAll('.trace-filters button[data-filter]').forEach(function (btn) {
        btn.addEventListener('click', function () {
            currentFilter = btn.getAttribute('data-filter');
            document.querySelectorAll('.trace-filters button[data-filter]').forEach(function (b) {
                b.classList.toggle('active', b === btn);
            });
            loadList();
        });
    });
    document.getElementById('btnRefresh').addEventListener('click', loadList);

    // Delegated list click: survives re-renders (10s polling rebuilds the
    // list; per-item listeners could be lost mid-click).
    document.getElementById('traceList').addEventListener('click', function (ev) {
        var item = ev.target && ev.target.closest ? ev.target.closest('.trace-item') : null;
        if (!item) return;
        var id = item.getAttribute('data-trace-id');
        if (id) selectTrace(id);
    });

    // Poll every 10s when on the list view (auto-refresh while agent runs)
    setInterval(function () {
        if (!document.hidden) loadList();
    }, 10000);

    loadList();

    // Deep link: /traces?trace=<id> opens that trace directly (from eval reports)
    var params = new URLSearchParams(window.location.search);
    var traceParam = params.get('trace');
    if (traceParam) {
        selectTrace(traceParam);
    }
})();
