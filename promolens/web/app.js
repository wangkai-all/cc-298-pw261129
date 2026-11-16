/* PromoLens 前端：无框架，直接操作 DOM。
   两个文件共享 window.PL。app.js 负责状态、条件控件与报告生命周期；
   views.js 负责结果渲染、图表、下钻、版本对比与数据页。 */
(function () {
  "use strict";

  const PL = (window.PL = {
    state: {
      boot: null,
      report: null,
      reportId: null,
      page: "analysis",
      pollTimer: null,
      draft: null,
      excludeIds: [],
      activeTab: "overview",
      storeFilter: { q: "", group: "all", onlyEligible: false },
      compare: { comboId: null, left: null, right: null },
      busy: false,
    },
    fmt: {},
    ui: {},
    views: {},
    actions: {},
  });

  // ------------------------------------------------------------------ util
  const fmt = PL.fmt;

  fmt.num = function (value, digits = 0) {
    if (value === null || value === undefined || Number.isNaN(Number(value))) return "—";
    return Number(value).toLocaleString("zh-CN", {
      minimumFractionDigits: digits,
      maximumFractionDigits: digits,
    });
  };
  fmt.money = function (value, digits = 2) {
    if (value === null || value === undefined) return "—";
    const n = Number(value);
    return (n > 0 ? "+" : "") + fmt.num(n, digits);
  };
  fmt.signed = function (value, digits = 2) {
    if (value === null || value === undefined) return "—";
    const n = Number(value);
    return (n > 0 ? "+" : "") + fmt.num(n, digits);
  };
  fmt.pct = function (value, digits = 1) {
    if (value === null || value === undefined) return "—";
    return fmt.num(Number(value) * 100, digits) + "%";
  };
  fmt.shortHash = function (hash, n = 10) {
    return hash ? hash.slice(0, n) : "—";
  };
  fmt.ts = function (iso) {
    if (!iso) return "—";
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return iso;
    const pad = (x) => String(x).padStart(2, "0");
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
  };
  fmt.group = function (group) {
    return group === "promo" ? "促销" : group === "control" ? "对照" : group || "—";
  };
  fmt.method = function (algorithm) {
    return { raw: "原始两组对照", matched: "倾向得分匹配", stratified: "精确分层对照" }[algorithm] || algorithm;
  };
  fmt.status = function (status) {
    return {
      queued: "排队中",
      computing: "计算中",
      final: "已完成",
      failed: "失败",
      superseded: "已被新版本取代",
    }[status] || status;
  };
  fmt.refundMode = function (mode) {
    return mode === "as_of_cutoff" ? "截至退款截止日" : "含快照内已到账退款";
  };

  const esc = (PL.esc = function (value) {
    return String(value === null || value === undefined ? "" : value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  });

  function h(tag, attrs, ...children) {
    const node = document.createElement(tag);
    if (attrs) {
      for (const [key, value] of Object.entries(attrs)) {
        if (value === null || value === undefined || value === false) continue;
        if (key === "class") node.className = value;
        else if (key === "html") node.innerHTML = value;
        else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
        else if (key === "dataset") Object.assign(node.dataset, value);
        else node.setAttribute(key, value === true ? "" : value);
      }
    }
    for (const child of children.flat()) {
      if (child === null || child === undefined || child === false) continue;
      node.append(child instanceof Node ? child : document.createTextNode(String(child)));
    }
    return node;
  }
  PL.h = h;

  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
  PL.$ = $;
  PL.$$ = $$;

  PL.api = async function (path, options) {
    const response = await fetch(path, {
      headers: { "Content-Type": "application/json" },
      ...options,
    });
    let body = null;
    const text = await response.text();
    try {
      body = text ? JSON.parse(text) : null;
    } catch (err) {
      body = { error: text };
    }
    if (!response.ok) {
      const message = (body && body.error) || `请求失败（${response.status}）`;
      throw new Error(message);
    }
    return body;
  };

  let toastTimer = null;
  PL.toast = function (message, isError = false) {
    const node = $("#toast");
    node.textContent = message;
    node.className = "toast show" + (isError ? " error" : "");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => {
      node.className = "toast";
    }, isError ? 6000 : 3200);
  };

  // ----------------------------------------------------------------- state
  PL.loadBootstrap = async function () {
    const boot = await PL.api("/api/bootstrap");
    PL.state.boot = boot;
    if (!PL.state.reportId) {
      const withCurrent = (boot.combos || []).find((c) => c.latest_report_id);
      if (withCurrent) PL.state.reportId = withCurrent.latest_report_id;
    }
    PL.renderChips();
    PL.renderControls();
    await PL.loadReport(PL.state.reportId, { quiet: true });
  };

  PL.renderChips = function () {
    const boot = PL.state.boot;
    const chips = $("#context-chips");
    if (!boot) return;
    chips.innerHTML = "";
    const report = PL.state.report;
    const snap = report
      ? { hash: report.snapshot_hash, at: report.spec ? report.created_at : null }
      : boot.latest_snapshot;
    if (snap) {
      chips.append(
        h(
          "span",
          { class: "chip", title: snap.hash || snap.snapshot_hash },
          h("span", { class: "dot ok" }),
          "快照 ",
          h("b", null, fmt.shortHash(snap.hash || snap.snapshot_hash))
        )
      );
    } else {
      chips.append(h("span", { class: "chip warn" }, h("span", { class: "dot" }), "尚无数据快照"));
    }
    if (report) {
      chips.append(
        h(
          "span",
          { class: "chip", title: report.rule_version },
          "规则 ",
          h("b", null, report.rule_version)
        ),
        h(
          "span",
          { class: "chip", title: report.cohort_version },
          "观察窗 ",
          h("b", null, report.cohort_version)
        ),
        h("span", { class: "chip" }, fmt.method(report.spec.method_algorithm)),
        h("span", { class: "chip" }, report.payload ? fmt.num(report.payload.headline.n_stores_promo + report.payload.headline.n_stores_control) + " 家门店" : fmt.status(report.status))
      );
      const stale = report.staleness || {};
      if (stale.rule_changed || stale.new_snapshot || stale.cohort_changed) {
        const bits = [];
        if (stale.rule_changed) bits.push("规则");
        if (stale.cohort_changed) bits.push("观察窗");
        if (stale.new_snapshot) bits.push("快照");
        chips.append(
          h(
            "span",
            { class: "chip warn", title: "当前启用版本或最新快照已与该报告不同；报告保持原样，重算会生成新版本。" },
            "有更新的" + bits.join("/")
          )
        );
      }
    }
    const running = boot.combos
      ? boot.combos.filter((c) => c.latest_status === "queued" || c.latest_status === "computing").length
      : 0;
    if (running) {
      chips.append(h("span", { class: "chip busy" }, h("span", { class: "dot" }), `${running} 个任务进行中`));
    }
  };

  // -------------------------------------------------------------- controls
  PL.renderControls = function () {
    const boot = PL.state.boot;
    if (!boot) return;
    const report = PL.state.report;
    const draft = PL.state.draft || (PL.state.draft = PL.draftFromReport(report || boot));
    $("#method-list").innerHTML = "";
    for (const method of boot.methods) {
      const selected = draft.method_algorithm === method.algorithm;
      const label = h(
        "label",
        { class: "method" + (selected ? " selected" : "") },
        h("input", {
          type: "radio",
          name: "method",
          value: method.algorithm,
          checked: selected,
          onchange: () => {
            PL.state.draft.method_algorithm = method.algorithm;
            PL.state.draft.method_params = { ...method.default_params };
            PL.renderControls();
          },
        }),
        h(
          "div",
          null,
          h("div", { class: "m-title" }, method.label),
          h("div", { class: "m-desc" }, method.description)
        )
      );
      $("#method-list").append(label);
    }
    PL.renderMethodParams();
    $("#refund-mode").value = draft.refund_mode;
    $("#threshold").value = draft.threshold;
    $("#pre-start").value = draft.pre_start;
    $("#pre-end").value = draft.pre_end;
    $("#camp-start").value = draft.campaign_start;
    $("#camp-end").value = draft.campaign_end;
    $("#refund-cutoff").value = draft.refund_cutoff;
    PL.renderPillGroup(
      $("#filter-regions"),
      PL.state.boot.store_regions || [],
      draft.filters.regions,
      (next) => {
        draft.filters.regions = next;
        PL.renderControls();
      }
    );
    PL.renderPillGroup(
      $("#filter-formats"),
      PL.state.boot.store_formats || [],
      draft.filters.formats,
      (next) => {
        draft.filters.formats = next;
        PL.renderControls();
      }
    );
    $("#lookback").value = draft.rule.lookback_hours;
    const statuses = $("#rule-statuses");
    statuses.innerHTML = "";
    for (const status of ["completed", "shipped", "pending", "cancelled", "returned"]) {
      statuses.append(
        h(
          "label",
          { class: "check" },
          h("input", {
            type: "checkbox",
            checked: draft.rule.order_statuses.includes(status),
            onchange: (e) => {
              draft.rule.order_statuses = toggle(draft.rule.order_statuses, status, e.target.checked);
              PL.updateDigestHint();
            },
          }),
          PL.statusLabel(status)
        )
      );
    }
    PL.renderPillGroup(
      $("#rule-channels"),
      ["app", "store", "wechat", "sms", "email"],
      draft.rule.channel_scope.includes("*") ? ["app", "store", "wechat", "sms", "email"] : draft.rule.channel_scope,
      (next) => {
        draft.rule.channel_scope = next.length ? next : ["*"];
        PL.renderControls();
      }
    );
    PL.renderPillGroup(
      $("#rule-kinds"),
      ["campaign", "regular"],
      draft.rule.exposure_kinds,
      (next) => {
        draft.rule.exposure_kinds = next;
        PL.renderControls();
      },
      { campaign: "促销曝光", regular: "常规曝光" }
    );
    $("#rule-same-store").checked = !!draft.rule.require_same_store;
    $("#rule-post").checked = !!draft.rule.post_exposure_only;
    for (const [id, key] of [
      ["#refund-mode", "refund_mode"],
      ["#threshold", "threshold"],
      ["#pre-start", "pre_start"],
      ["#pre-end", "pre_end"],
      ["#camp-start", "campaign_start"],
      ["#camp-end", "campaign_end"],
      ["#refund-cutoff", "refund_cutoff"],
    ]) {
      const input = $(id);
      input.onchange = () => {
        const value = key === "threshold" ? Number(input.value) : input.value;
        draft[key] = value;
        PL.updateDigestHint();
      };
    }
    $("#lookback").onchange = (e) => {
      draft.rule.lookback_hours = Number(e.target.value);
      PL.updateDigestHint();
    };
    $("#rule-same-store").onchange = (e) => {
      draft.rule.require_same_store = e.target.checked;
      PL.updateDigestHint();
    };
    $("#rule-post").onchange = (e) => {
      draft.rule.post_exposure_only = e.target.checked;
      PL.updateDigestHint();
    };
    const excludes = $("#manual-excludes");
    if (PL.state.excludeIds.length) {
      excludes.innerHTML = "";
      for (const id of PL.state.excludeIds) {
        excludes.append(
          h(
            "div",
            { class: "check", style: "justify-content:space-between" },
            h("span", null, id),
            h(
              "button",
              {
                class: "btn ghost small",
                onclick: () => {
                  PL.state.excludeIds = PL.state.excludeIds.filter((x) => x !== id);
                  draft.filters.exclude_store_ids = PL.state.excludeIds;
                  PL.renderControls();
                },
              },
              "移除"
            )
          )
        );
      }
    } else {
      excludes.textContent = "在上方“门店明细”中点选门店后可手动排除。";
    }
    PL.updateDigestHint();
  };

  PL.statusLabel = function (status) {
    return {
      completed: "已完成",
      shipped: "已发货",
      pending: "待处理",
      cancelled: "已取消",
      returned: "已退货",
    }[status] || status;
  };

  function toggle(list, value, on) {
    const next = new Set(list);
    if (on) next.add(value);
    else next.delete(value);
    return Array.from(next);
  }

  PL.renderPillGroup = function (root, values, selected, onChange, labels) {
    root.innerHTML = "";
    for (const value of values) {
      const on = selected.includes(value);
      const label = (labels && labels[value]) || PL.channelLabel(value);
      root.append(
        h(
          "button",
          {
            type: "button",
            class: "pill-check" + (on ? " on" : ""),
            "aria-pressed": on ? "true" : "false",
            onclick: () => onChange(on ? selected.filter((x) => x !== value) : selected.concat([value])),
          },
          label
        )
      );
    }
  };

  PL.channelLabel = function (channel) {
    return { app: "App", store: "门店", wechat: "微信", sms: "短信", email: "邮件", "*": "全部渠道" }[channel] || channel;
  };

  PL.renderMethodParams = function () {
    const draft = PL.state.draft;
    const root = $("#method-params");
    root.innerHTML = "";
    const wrap = h("div", { class: "field", style: "margin-top:9px" });
    if (draft.method_algorithm === "matched") {
      wrap.append(
        h("label", null, "卡钳宽度（倾向得分的标准差倍数）"),
        h("input", {
          type: "number",
          min: "0.05",
          max: "1",
          step: "0.05",
          value: draft.method_params.caliper_sd || 0.2,
          onchange: (e) => {
            draft.method_params.caliper_sd = Number(e.target.value);
            PL.updateDigestHint();
          },
        }),
        h(
          "label",
          { class: "check", style: "margin-top:5px" },
          h("input", {
            type: "checkbox",
            checked: !!draft.method_params.with_replacement,
            onchange: (e) => {
              draft.method_params.with_replacement = e.target.checked;
              PL.updateDigestHint();
            },
          }),
          "允许对照门店重复使用"
        ),
        h(
          "p",
          { style: "font-size:11px;color:var(--muted);margin:4px 0 0" },
          "卡钳越小越可比，但被排除的促销门店越多。"
        )
      );
    } else if (draft.method_algorithm === "stratified") {
      wrap.append(h("label", null, "分层维度"));
      const dims = draft.method_params.strata_dims || ["region", "format"];
      const holder = h("div", { class: "multi" });
      for (const [key, label] of [["region", "区域"], ["format", "业态"]]) {
        const on = dims.includes(key);
        holder.append(
          h(
            "button",
            {
              type: "button",
              class: "pill-check" + (on ? " on" : ""),
              onclick: () => {
                const next = on ? dims.filter((d) => d !== key) : dims.concat([key]);
                if (!next.length) {
                  PL.toast("至少保留一个分层维度", true);
                  return;
                }
                draft.method_params.strata_dims = next;
                PL.renderControls();
              },
            },
            label
          )
        );
      }
      wrap.append(holder);
      wrap.append(
        h(
          "p",
          { style: "font-size:11px;color:var(--muted);margin:4px 0 0" },
          "只用两组都存在的层；单臂层会被排除并列出。"
        )
      );
    } else {
      wrap.append(
        h(
          "p",
          { style: "font-size:11px;color:var(--muted);margin:0" },
          "不做平衡调整：保留全部合格门店，差异同时包含分组选择偏差。"
        )
      );
    }
    root.append(wrap);
  };

  PL.draftFromReport = function (source) {
    if (source && source.spec) {
      const spec = source.spec;
      return {
        method_algorithm: spec.method_algorithm,
        method_params: JSON.parse(JSON.stringify(spec.method_params || {})),
        threshold: spec.threshold,
        refund_mode: spec.cohort.refund_mode,
        pre_start: spec.cohort.pre_start,
        pre_end: spec.cohort.pre_end,
        campaign_start: spec.cohort.campaign_start,
        campaign_end: spec.cohort.campaign_end,
        refund_cutoff: spec.cohort.refund_cutoff,
        rule: {
          label: spec.rule.label,
          lookback_hours: spec.rule.lookback_hours,
          order_statuses: spec.rule.order_statuses.slice(),
          channel_scope: spec.rule.channel_scope.slice(),
          exposure_kinds: spec.rule.exposure_kinds.slice(),
          require_same_store: spec.rule.require_same_store,
          post_exposure_only: spec.rule.post_exposure_only,
          require_gross_cents: spec.rule.require_gross_cents,
        },
        filters: {
          regions: (spec.filters && spec.filters.regions) || [],
          formats: (spec.filters && spec.filters.formats) || [],
          exclude_store_ids: PL.state.excludeIds.slice(),
        },
        cohort_label: spec.cohort.label,
        rule_version: source.rule_version,
        cohort_version: source.cohort_version,
      };
    }
    const boot = PL.state.boot || {};
    const rule = boot.active_rule || {};
    const cohort = boot.active_cohort || {};
    return {
      method_algorithm: "raw",
      method_params: {},
      threshold: (boot.limits && boot.limits.min_orders_per_window) || 30,
      refund_mode: cohort.refund_mode || "as_of_cutoff",
      pre_start: cohort.pre_start || "",
      pre_end: cohort.pre_end || "",
      campaign_start: cohort.campaign_start || "",
      campaign_end: cohort.campaign_end || "",
      refund_cutoff: cohort.refund_cutoff || "",
      rule: {
        label: rule.label || "默认归因规则",
        lookback_hours: rule.lookback_hours || 48,
        order_statuses: (rule.order_statuses || ["completed", "shipped"]).slice(),
        channel_scope: (rule.channel_scope || ["*"]).slice(),
        exposure_kinds: (rule.exposure_kinds || ["campaign", "regular"]).slice(),
        require_same_store: rule.require_same_store !== false,
        post_exposure_only: rule.post_exposure_only !== false,
        require_gross_cents: rule.require_gross_cents !== false,
      },
      filters: { regions: [], formats: [], exclude_store_ids: [] },
      rule_version: rule.rule_version,
      cohort_version: cohort.cohort_version,
    };
  };

  PL.updateDigestHint = function () {
    const draft = PL.state.draft;
    const report = PL.state.report;
    const node = $("#input-digest-hint");
    if (!draft) return;
    const changed = [];
    if (report && report.spec) {
      if (draft.method_algorithm !== report.spec.method_algorithm) changed.push("方法");
      else if (JSON.stringify(draft.method_params) !== JSON.stringify(report.spec.method_params)) changed.push("方法参数");
      if (Number(draft.threshold) !== Number(report.spec.threshold)) changed.push("订单门槛");
      if (draft.refund_mode !== report.spec.cohort.refund_mode) changed.push("退款口径");
      if (
        draft.pre_start !== report.spec.cohort.pre_start ||
        draft.pre_end !== report.spec.cohort.pre_end ||
        draft.campaign_start !== report.spec.cohort.campaign_start ||
        draft.campaign_end !== report.spec.cohort.campaign_end ||
        draft.refund_cutoff !== report.spec.cohort.refund_cutoff
      )
        changed.push("观察窗");
      const ruleKeys = ["lookback_hours", "order_statuses", "channel_scope", "exposure_kinds", "require_same_store", "post_exposure_only"];
      if (ruleKeys.some((k) => JSON.stringify(draft.rule[k]) !== JSON.stringify(report.spec.rule[k]))) changed.push("归因规则");
      if (JSON.stringify(draft.filters) !== JSON.stringify(report.spec.filters)) changed.push("筛选");
      node.textContent = changed.length
        ? "待重算：" + changed.join("、") + "（保存为新的输入版本）"
        : `当前条件与 ${report.report_id} 一致 · 输入摘要 ${fmt.shortHash(report.inputs_digest, 12)}`;
      node.style.color = changed.length ? "var(--est)" : "var(--muted)";
    } else {
      node.textContent = "首次运行会创建规则/观察窗版本并冻结快照";
      node.style.color = "var(--muted)";
    }
  };

  // ------------------------------------------------------------- lifecycle
  PL.loadReport = async function (reportId, options = {}) {
    if (!reportId) {
      PL.state.report = null;
      PL.renderChips();
      PL.views.renderAnalysis();
      return;
    }
    const report = await PL.api(`/api/reports/${reportId}`);
    PL.state.report = report;
    PL.state.reportId = report.report_id;
    if (!options.keepDraft) {
      PL.state.excludeIds = (report.spec.filters.exclude_store_ids || []).slice();
      PL.state.draft = PL.draftFromReport(report);
    }
    PL.renderChips();
    PL.renderControls();
    PL.views.renderAnalysis();
    if (report.status === "queued" || report.status === "computing") {
      PL.pollReport(report.report_id);
    } else {
      PL.stopPoll();
    }
    if (!options.quiet) PL.views.renderCompare();
  };

  PL.pollReport = function (reportId) {
    PL.stopPoll();
    PL.state.pollTimer = setTimeout(async () => {
      try {
        const report = await PL.api(`/api/reports/${reportId}`);
        if (PL.state.reportId === reportId) {
          PL.state.report = report;
          PL.renderChips();
          PL.views.renderAnalysis();
        }
        if (report.status === "queued" || report.status === "computing") {
          PL.pollReport(reportId);
        } else if (report.status === "final") {
          PL.toast(`报告 v${report.version_number} 已完成`);
        } else if (report.status === "failed") {
          PL.toast("报告计算失败：" + ((report.runs.slice(-1)[0] || {}).error || "未知错误"), true);
        }
      } catch (err) {
        PL.toast(err.message, true);
      }
    }, 900);
  };

  PL.stopPoll = function () {
    if (PL.state.pollTimer) clearTimeout(PL.state.pollTimer);
    PL.state.pollTimer = null;
  };

  PL.actions.run = async function () {
    const draft = PL.state.draft;
    if (PL.state.busy) return;
    const btn = $("#run-btn");
    PL.state.busy = true;
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner"></span> 提交中…';
    try {
      const currentReport = PL.state.report;
      const payload = {
        method: draft.method_algorithm,
        method_params: draft.method_params,
        threshold: Number(draft.threshold),
        filters: draft.filters,
        label: undefined,
      };
      const ruleChanged =
        !currentReport ||
        draft.rule.lookback_hours !== currentReport.spec.rule.lookback_hours ||
        JSON.stringify([...draft.rule.order_statuses].sort()) !== JSON.stringify([...currentReport.spec.rule.order_statuses].sort()) ||
        JSON.stringify([...draft.rule.channel_scope].sort()) !== JSON.stringify([...currentReport.spec.rule.channel_scope].sort()) ||
        JSON.stringify([...draft.rule.exposure_kinds].sort()) !== JSON.stringify([...currentReport.spec.rule.exposure_kinds].sort()) ||
        !!draft.rule.require_same_store !== !!currentReport.spec.rule.require_same_store ||
        !!draft.rule.post_exposure_only !== !!currentReport.spec.rule.post_exposure_only ||
        Number(draft.rule.lookback_hours) !== Number(currentReport.spec.rule.lookback_hours);
      if (ruleChanged) {
        payload.rule_overrides = {
          label: `${draft.rule.label}·${fmt.ts(new Date().toISOString()).slice(0, 16)}`,
          lookback_hours: draft.rule.lookback_hours,
          order_statuses: draft.rule.order_statuses,
          channel_scope: draft.rule.channel_scope,
          exposure_kinds: draft.rule.exposure_kinds,
          require_same_store: draft.rule.require_same_store,
          post_exposure_only: draft.rule.post_exposure_only,
          require_gross_cents: draft.rule.require_gross_cents,
        };
      } else if (currentReport) {
        payload.rule_version = currentReport.rule_version;
      }
      const cohortChanged =
        !currentReport ||
        draft.refund_mode !== currentReport.spec.cohort.refund_mode ||
        draft.pre_start !== currentReport.spec.cohort.pre_start ||
        draft.pre_end !== currentReport.spec.cohort.pre_end ||
        draft.campaign_start !== currentReport.spec.cohort.campaign_start ||
        draft.campaign_end !== currentReport.spec.cohort.campaign_end ||
        draft.refund_cutoff !== currentReport.spec.cohort.refund_cutoff;
      if (cohortChanged) {
        payload.cohort_overrides = {
          label: `${draft.cohort_label || currentReport?.spec.cohort.label || "观察窗"}·${fmt.ts(new Date().toISOString()).slice(0, 16)}`,
          campaign_start: draft.campaign_start,
          campaign_end: draft.campaign_end,
          pre_start: draft.pre_start,
          pre_end: draft.pre_end,
          refund_cutoff: draft.refund_cutoff,
          refund_mode: draft.refund_mode,
        };
      } else if (currentReport) {
        payload.cohort_version = currentReport.cohort_version;
      }
      const created = await PL.api("/api/reports", { method: "POST", body: JSON.stringify(payload) });
      PL.toast(`已入队 v${created.version_number}，正在计算…`);
      PL.state.report = created;
      PL.state.reportId = created.report_id;
      PL.state.draft = PL.draftFromReport(created);
      PL.state.excludeIds = (created.spec.filters.exclude_store_ids || []).slice();
      PL.renderChips();
      PL.views.renderAnalysis();
      PL.pollReport(created.report_id);
      const boot = await PL.api("/api/bootstrap");
      PL.state.boot = boot;
      PL.renderChips();
    } catch (err) {
      PL.toast(err.message, true);
    } finally {
      PL.state.busy = false;
      btn.disabled = false;
      btn.textContent = "重算并新建版本";
    }
  };

  PL.actions.resetDraft = function () {
    PL.state.excludeIds = [];
    PL.state.draft = PL.draftFromReport(PL.state.report);
    PL.renderControls();
  };

  // ---------------------------------------------------------------- router
  PL.setPage = function (page) {
    PL.state.page = page;
    for (const button of $$(".nav button")) {
      button.setAttribute("aria-selected", button.dataset.page === page ? "true" : "false");
    }
    $("#page-analysis").hidden = page !== "analysis";
    $("#page-compare").hidden = page !== "compare";
    $("#page-data").hidden = page !== "data";
    if (page === "compare") PL.views.renderCompare();
    if (page === "data") PL.views.renderData();
  };

  // ------------------------------------------------------------------ init
  function wire() {
    $("#run-btn").addEventListener("click", () => PL.actions.run());
    $("#reset-btn").addEventListener("click", () => PL.actions.resetDraft());
    for (const button of $$(".nav button")) {
      button.addEventListener("click", () => PL.setPage(button.dataset.page));
    }
    for (const tab of $$("#results .tabs button")) {
      tab.addEventListener("click", () => PL.views.setTab(tab.dataset.tab));
    }
    $("#drawer-close").addEventListener("click", () => PL.views.closeDrawer());
    $("#drawer-scrim").addEventListener("click", () => PL.views.closeDrawer());
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape") PL.views.closeDrawer();
    });
  }

  document.addEventListener("DOMContentLoaded", async () => {
    wire();
    try {
      await PL.loadBootstrap();
    } catch (err) {
      PL.toast("初始化失败：" + err.message, true);
      $("#results").innerHTML = `<div class="empty"><h3>无法连接后端</h3><p>${esc(err.message)}</p></div>`;
    }
  });
})();
