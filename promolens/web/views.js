/* PromoLens 视图层：结果渲染、图表、下钻、版本对比、数据页。 */
(function () {
  "use strict";
  const PL = window.PL;
  if (!PL) throw new Error("app.js 必须先加载");
  const { h, esc, fmt, api } = PL;
  const $ = PL.$;

  const COLORS = {
    promo: "#0c6b5c",
    control: "#6a5a91",
    est: "#a8530b",
    raw: "#8b9099",
    grid: "#e7e4dc",
    ink: "#3f464d",
    muted: "#6b7178",
    accentSoft: "#fbeee1",
  };
  const views = PL.views;

  // ------------------------------------------------------------------ utils
  function warning(htmlText, klass) {
    return h("div", { class: "note-box" + (klass ? " " + klass : ""), html: htmlText });
  }

  function statusBadge(status) {
    return h("span", { class: "status-badge " + status }, fmt.status(status));
  }

  function groupTag(group) {
    return h("span", { class: "tag " + (group === "promo" ? "promo" : "control") }, fmt.group(group));
  }

  function emptyBox(title, text) {
    return h("div", { class: "empty" }, h("h3", null, title), h("p", null, text));
  }

  function kpi(label, value, sub, klass, title) {
    return h(
      "div",
      { class: "kpi" + (klass ? " " + klass : ""), title: title || null },
      h("div", { class: "k-label" }, label),
      h("div", { class: "k-value", html: value }),
      sub ? h("div", { class: "k-sub", html: sub }) : null
    );
  }

  function svgEl(tag, attrs, ...children) {
    const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
    for (const [key, value] of Object.entries(attrs || {})) {
      if (value === null || value === undefined) continue;
      node.setAttribute(key, String(value));
    }
    for (const child of children.flat(Infinity)) {
      if (child === null || child === undefined || child === false) continue;
      node.append(child instanceof Node ? child : document.createTextNode(String(child)));
    }
    return node;
  }

  function niceExtent(values, padRatio = 0.08) {
    const finite = values.filter((v) => Number.isFinite(v));
    if (!finite.length) return [0, 1];
    let lo = Math.min(...finite);
    let hi = Math.max(...finite);
    if (lo === hi) {
      lo -= 1;
      hi += 1;
    }
    const pad = (hi - lo) * padRatio;
    return [lo - pad, hi + pad];
  }

  function ticks(lo, hi, count = 5) {
    const span = hi - lo;
    if (!Number.isFinite(span) || span <= 0) return [lo];
    const rough = span / count;
    const magnitude = Math.pow(10, Math.floor(Math.log10(rough)));
    const candidates = [1, 2, 2.5, 5, 10].map((m) => m * magnitude);
    const step = candidates.find((c) => span / c <= count + 1) || magnitude * 10;
    const start = Math.ceil(lo / step) * step;
    const out = [];
    for (let v = start; v <= hi + step * 1e-9; v += step) out.push(Math.round(v * 1e6) / 1e6);
    return out;
  }

  // ------------------------------------------------------------------ tabs
  const TABS = [
    ["overview", "总览"],
    ["balance", "可比性与支持"],
    ["stores", "门店明细"],
    ["coverage", "排除样本"],
    ["attribution", "曝光归因"],
    ["sensitivity", "敏感性"],
    ["evidence", "依据与版本"],
  ];

  views.setTab = function (tab) {
    PL.state.activeTab = tab;
    renderAnalysis();
  };

  function renderTabs(active) {
    return h(
      "div",
      { class: "tabs", role: "tablist" },
      ...TABS.map(([key, label]) =>
        h(
          "button",
          {
            role: "tab",
            dataset: { tab: key },
            "aria-selected": key === active ? "true" : "false",
            onclick: () => views.setTab(key),
          },
          label
        )
      )
    );
  }

  // =============================================================== ANALYSIS
  views.renderAnalysis = renderAnalysis;

  function renderAnalysis() {
    const root = $("#results");
    const report = PL.state.report;
    if (!report) {
      root.innerHTML = "";
      root.append(
        emptyBox(
          "还没有报告",
          "左侧确认条件后点“重算并新建版本”。报告会冻结当前数据快照、规则、观察窗、方法与参数。"
        )
      );
      return;
    }
    root.innerHTML = "";
    root.append(renderReportBar(report));
    const stale = report.staleness || {};
    if (stale.rule_changed || stale.cohort_changed || stale.new_snapshot) {
      const bits = [];
      if (stale.rule_changed) bits.push(`归因规则（当前启用 <span class="mono">${esc(stale.active_rule_version)}</span>）`);
      if (stale.cohort_changed) bits.push(`观察窗（当前启用 <span class="mono">${esc(stale.active_cohort_version)}</span>）`);
      if (stale.new_snapshot) bits.push(`数据快照（最新 <span class="mono">${esc(fmt.shortHash(stale.latest_snapshot_hash))}</span>）`);
      root.append(
        warning(
          `这份报告之后出现了更新的${bits.join("、")}。<strong>本报告不会自动变化</strong>；它仍然读自己绑定的输入。` +
            `要纳入新条件或回补后的数据，请重算生成下一版。`
        )
      );
    }
    if (report.status === "queued" || report.status === "computing") {
      root.append(renderPending(report));
      return;
    }
    if (report.status === "failed") {
      root.append(renderFailed(report));
      return;
    }
    const panel = h("div", { class: "panel" });
    panel.append(renderTabs(PL.state.activeTab));
    const body = h("div", { class: "tab-panel" });
    const payload = report.payload;
    switch (PL.state.activeTab) {
      case "balance":
        body.append(renderBalanceTab(payload));
        break;
      case "stores":
        body.append(renderStoresTab(payload));
        break;
      case "coverage":
        body.append(renderCoverageTab(payload));
        break;
      case "sensitivity":
        body.append(renderSensitivityTab(payload));
        break;
      case "attribution":
        body.append(renderAttributionTab(report));
        break;
      case "evidence":
        body.append(renderEvidenceTab(report));
        break;
      default:
        body.append(renderOverviewTab(payload));
    }
    panel.append(body);
    root.append(panel);
    if (report.status !== "final" && report.status !== "failed") {
      root.append(
        warning(
          `报告状态为 <strong>${esc(fmt.status(report.status))}</strong>，结果仅供参考；完成后会刷新。`
        )
      );
    }
  }

  function renderReportBar(report) {
    const bar = h("div", { class: "report-bar" });
    const title = h(
      "div",
      null,
      h(
        "div",
        { class: "rb-title" },
        `${report.combo_label || "分析组合"} · v${report.version_number} `,
        statusBadge(report.status)
      ),
      h(
        "div",
        { class: "rb-meta" },
        h("span", null, `生成 ${fmt.ts(report.created_at)}`),
        h("span", null, `方法 ${report.spec.method.label}`),
        h("span", null, `口径 ${fmt.refundMode(report.spec.cohort.refund_mode)}`),
        h("span", { title: report.inputs_digest }, `输入 ${fmt.shortHash(report.inputs_digest, 12)}`),
        report.payload_digest
          ? h("span", { title: report.payload_digest }, `结果 ${fmt.shortHash(report.payload_digest, 12)}`)
          : null
      )
    );
    bar.append(title);
    const actions = h("div", { class: "rb-actions" });
    if (report.status === "final") {
      actions.append(
        h(
          "a",
          {
            class: "btn small",
            href: `/api/reports/${report.report_id}/evidence`,
            download: "",
            title: "包含完整输入、方法版本、结果与复现参数",
          },
          "导出依据 JSON"
        ),
        h(
          "a",
          {
            class: "btn small",
            href: `/api/reports/${report.report_id}/stores.csv`,
            download: "",
          },
          "门店 CSV"
        ),
        h(
          "a",
          {
            class: "btn small",
            href: `/api/reports/${report.report_id}/methodology.md`,
            download: "",
            title: "方法、假设、排除样本与敏感性",
          },
          "方法说明"
        )
      );
    }
    actions.append(
      h(
        "button",
        {
          class: "btn small primary",
          onclick: () => PL.actions.run(),
          title: "用左侧当前条件生成新的组合版本",
        },
        "重算并新建版本"
      )
    );
    if (report.version_number > 1 || report.combo_id) {
      actions.append(
        h(
          "button",
          {
            class: "btn small",
            onclick: () => {
              PL.state.compare.left = report.report_id;
              PL.setPage("compare");
            },
          },
          "去对比"
        )
      );
    }
    bar.append(actions);
    return bar;
  }

  function renderPending(report) {
    const panel = h("div", { class: "panel" }, h("div", { class: "tab-panel" }));
    panel.firstChild.append(
      h(
        "div",
        { class: "flow" },
        h(
          "div",
          { class: "flow-step" },
          h("div", { class: "n" }, "1"),
          h(
            "div",
            { class: "b" },
            h("b", null, "已冻结输入"),
            `快照 ${fmt.shortHash(report.snapshot_hash)}，规则 ${report.rule_version}，观察窗 ${report.cohort_version}，方法 ${report.method_version}`
          )
        ),
        h(
          "div",
          { class: "flow-step" },
          h("div", { class: "n" }, "2"),
          h(
            "div",
            { class: "b" },
            h("b", null, "正在计算"),
            "该任务只会写入自己这一版报告；同组合的新版本会使它作废，旧结果不会覆盖新报告。"
          )
        ),
        h(
          "div",
          { class: "flow-step" },
          h("div", { class: "n" }, "3"),
          h("div", { class: "b" }, h("b", null, "完成后可下钻与导出"), "页面会自动刷新。")
        )
      ),
      h(
        "div",
        { class: "stat-strip", style: "margin-top:14px" },
        h("div", { class: "s" }, h("div", { class: "l" }, "状态"), h("div", { class: "v" }, fmt.status(report.status))),
        h("div", { class: "s" }, h("div", { class: "l" }, "运行记录"), h("div", { class: "v" }, String(report.runs.length))),
        h("div", { class: "s" }, h("div", { class: "l" }, "输入摘要"), h("div", { class: "v mono" }, fmt.shortHash(report.inputs_digest, 16)))
      ),
      h(
        "div",
        { style: "margin-top:12px" },
        ...report.events.map((e) =>
          h("div", { class: "reject-row" }, h("span", { class: "mono" }, fmt.ts(e.at)), " ", esc(e.detail))
        )
      )
    );
    return panel;
  }

  function renderFailed(report) {
    const lastRun = report.runs[report.runs.length - 1] || {};
    return h(
      "div",
      { class: "panel" },
      h(
        "div",
        { class: "tab-panel" },
        warning(`计算失败：<strong>${esc(lastRun.error || "未知错误")}</strong>`, "warn"),
        h(
          "p",
          { style: "margin-top:10px;color:var(--muted);font-size:12px" },
          "失败不会改动任何已完成报告。确认输入后可以重算，新结果会是同一组合的下一个版本。"
        )
      )
    );
  }

  // ------------------------------------------------------------- overview
  function renderOverviewTab(payload) {
    const wrap = h("div");
    const hl = payload.headline;
    const ci = hl.ci || {};
    const balance = payload.balance_summary;
    const refund = payload.refund_quality;
    const methodNote = {
      raw: "未调整：两组差异同时包含促销效果与门店选择偏差。",
      matched: "匹配后：仅在倾向得分重叠且卡钳内的门店之间比较。",
      stratified: "分层后：仅在两组都存在的层内比较。",
    }[hl.method_algorithm];

    wrap.append(
      h(
        "div",
        { class: "kpis" },
        kpi(
          "原始两组差异",
          `${fmt.signed(hl.raw_difference)}<small> 元/会员</small>`,
          "不调整门店构成",
          "raw",
          "促销组每会员净销售变化减去对照组，未做任何可比性调整。"
        ),
        kpi(
          `${hl.method_label}估计`,
          `${fmt.signed(hl.adjusted_estimate)}<small> 元/会员</small>`,
          `相对主估计 ${fmt.signed(hl.adjustment_delta)}`,
          "accent",
          "这是方法假设下的关联差异，不是随机试验的提升。"
        ),
        kpi(
          "区间（门店重抽样）",
          `${fmt.money(ci.ci_low)} ~ ${fmt.signed(ci.ci_high)}`,
          `${ci.reps || 0} 次 · 百分位 95%`,
          "",
          ci.note || ""
        ),
        kpi(
          "进入比较的门店",
          `${fmt.num(hl.n_stores_promo)} <small>促销</small> / ${fmt.num(hl.n_stores_control)} <small>对照</small>`,
          `会员 ${fmt.num(hl.n_members_promo)} / ${fmt.num(hl.n_members_control)}`
        )
      )
    );

    wrap.append(
      warning(
        `<strong>不要把相关性写成提升。</strong>${esc(payload.caveat)}` +
          `<br><span style="color:var(--ink-soft)">${esc(hl.interpretation)}</span>` +
          `<br><span style="color:var(--muted)">方法：${esc(methodNote)} 估计量：${esc(payload.estimand)}</span>`
      )
    );

    // two-arm rates visual
    const promo = payload.arms.promo;
    const control = payload.arms.control;
    const rawCard = h(
      "div",
      { class: "compare-raw" },
      h(
        "div",
        { class: "cr-cell" },
        h("div", { class: "t" }, "对照门店 · 每会员活动期净销售"),
        h("div", { class: "v", style: "color:var(--control)" }, `${fmt.num(control.campaign_rate_mean, 2)} 元`),
        h(
          "div",
          { class: "s" },
          `${fmt.num(control.stores)} 家 · 期前 ${fmt.num(control.pre_rate_mean, 2)} 元 · 变化 ${fmt.signed(control.outcome_mean, 2)}`
        )
      ),
      h("div", { class: "cr-mid" }, "原始差异"),
      h(
        "div",
        { class: "cr-cell" },
        h("div", { class: "t" }, "促销门店 · 每会员活动期净销售"),
        h("div", { class: "v", style: "color:var(--promo)" }, `${fmt.num(promo.campaign_rate_mean, 2)} 元`),
        h(
          "div",
          { class: "s" },
          `${fmt.num(promo.stores)} 家 · 期前 ${fmt.num(promo.pre_rate_mean, 2)} 元 · 变化 ${fmt.signed(promo.outcome_mean, 2)}`
        )
      )
    );
    wrap.append(h("div", { class: "subhead" }, h("h3", null, "两组水平与原始差异"), h("span", { class: "hint" }, "元/会员/28 天，已按窗口长度折算")), rawCard);

    wrap.append(
      h(
        "div",
        { class: "stat-strip", style: "margin-top:12px" },
        h(
          "div",
          { class: "s" },
          h("div", { class: "l" }, "调整前后不平衡"),
          h(
            "div",
            { class: "v" },
            `${fmt.num(balance.max_abs_smd_unadjusted, 2)} → ${fmt.num(balance.max_abs_smd_adjusted, 2)}`,
            h("span", { style: "font-size:11px;color:var(--muted);font-weight:500" }, " 最大 |SMD|")
          )
        ),
        h(
          "div",
          { class: "s" },
          h("div", { class: "l" }, "进入比较的样本"),
          h(
            "div",
            { class: "v" },
            `${payload.exclusions.counts.stores_eligible} / ${payload.exclusions.counts.stores_total}`,
            h("span", { style: "font-size:11px;color:var(--muted);font-weight:500" }, " 家门店")
          )
        ),
        h(
          "div",
          { class: "s" },
          h("div", { class: "l" }, "方法额外排除"),
          h(
            "div",
            { class: "v" },
            String(payload.exclusions.method_level.length),
            h("span", { style: "font-size:11px;color:var(--muted);font-weight:500" }, " 家")
          )
        ),
        h(
          "div",
          { class: "s" },
          h("div", { class: "l" }, "退款截止后到账"),
          h(
            "div",
            { class: "v" },
            `${fmt.num(refund.refunds_after_cutoff_in_snapshot)} 笔`,
            h(
              "span",
              { style: "font-size:11px;color:var(--muted);font-weight:500" },
              refund.late_refunds_in_snapshot_affect_primary ? " 已含在当前口径" : " 未进入当前口径"
            )
          )
        ),
        h(
          "div",
          { class: "s" },
          h("div", { class: "l" }, "归因订单占比"),
          h(
            "div",
            { class: "v" },
            fmt.pct(promo.attributed_share_mean, 1),
            h("span", { style: "font-size:11px;color:var(--muted);font-weight:500" }, " 促销组")
          )
        )
      )
    );

    // sensitivity teaser
    const sens = payload.sensitivity || [];
    if (sens.length) {
      wrap.append(
        h(
          "div",
          { class: "subhead" },
          h("h3", null, "选择不同做法时的结果"),
          h("span", { class: "hint" }, "同一快照内重算；不代表误差范围")
        )
      );
      wrap.append(renderSensitivityChart(payload, { compact: true }));
    }

    const notes = [];
    if (balance.max_abs_smd_adjusted > 0.1) {
      notes.push(
        `调整后仍有协变量 |SMD| 达到 ${fmt.num(balance.max_abs_smd_adjusted, 2)}（常用参考阈值 0.1），说明两组在已观测特征上仍未完全可比；跨方法解读时不要忽略这一点。`
      );
    } else if (balance.improved) {
      notes.push("调整后已观测特征的不平衡下降，但未观测因素无法用这些协变量检验。");
    }
    if (refund.refunds_after_cutoff_in_snapshot > 0 && !refund.late_refunds_in_snapshot_affect_primary) {
      notes.push(
        `快照中有 ${refund.refunds_after_cutoff_in_snapshot} 笔退款发生在截止日之后，当前口径未计入；若改用“包含已到账退款”，估计变为 ${fmt.signed(refund.sensitivity_estimate_other_mode)}。`
      );
    }
    if (payload.exclusions.row_level.orders_excluded_status) {
      notes.push(
        `按当前归因规则，${fmt.num(payload.exclusions.row_level.orders_excluded_status)} 笔订单因状态不符被排除，${fmt.num(payload.exclusions.row_level.orders_excluded_channel)} 笔因渠道不在范围内被排除。`
      );
    }
    if (notes.length) {
      wrap.append(
        h("div", { class: "subhead" }, h("h3", null, "读这个结果时要留意")),
        h("ul", { style: "margin:0;padding-left:18px;color:var(--ink-soft);font-size:12px;line-height:1.6" }, ...notes.map((n) => h("li", null, n)))
      );
    }
    return wrap;
  }

  // -------------------------------------------------------------- balance
  function renderBalanceTab(payload) {
    const wrap = h("div");
    const support = payload.support || {};
    wrap.append(
      h(
        "div",
        { class: "subhead" },
        h("h3", null, "已观测特征的平衡"),
        h("span", { class: "hint" }, "标准化均值差（SMD）；绝对值越小越可比")
      )
    );
    wrap.append(renderSmdChart(payload.balance));
    wrap.append(
      h(
        "div",
        { class: "table-wrap short", style: "margin-top:10px" },
        h(
          "table",
          { class: "grid" },
          h(
            "thead",
            null,
            h(
              "tr",
              null,
              h("th", null, "特征"),
              h("th", { class: "r" }, "促销组"),
              h("th", { class: "r" }, "对照组"),
              h("th", { class: "r" }, "调整前 |SMD|"),
              h("th", { class: "r" }, "调整后 |SMD|")
            )
          ),
          h(
            "tbody",
            null,
            ...payload.balance.map((row) =>
              h(
                "tr",
                null,
                h("td", null, row.label),
                h("td", { class: "r" }, fmt.num(row.promo_mean, 3)),
                h("td", { class: "r" }, fmt.num(row.control_mean, 3)),
                h("td", { class: "r" }, fmt.num(Math.abs(row.smd_unadjusted), 3)),
                h(
                  "td",
                  { class: "r", style: Math.abs(row.smd_adjusted) > 0.1 ? "color:var(--est);font-weight:600" : "" },
                  fmt.num(Math.abs(row.smd_adjusted), 3)
                )
              )
            )
          )
        )
      )
    );

    wrap.append(
      h("div", { class: "subhead" }, h("h3", null, "共同支持与样本取舍"), h("span", { class: "hint" }, support.note || "")),
      h("div", { class: "cards", style: "grid-template-columns:repeat(auto-fit,minmax(220px,1fr))" }, renderSupportCards(payload))
    );

    if (payload.strata && payload.strata.length) {
      wrap.append(
        h(
          "div",
          { class: "subhead" },
          h("h3", null, "各层对照"),
          h("span", { class: "hint" }, `使用 ${payload.strata.length} 个层；权重按会员数`)
        )
      );
      wrap.append(
        h(
          "div",
          { class: "table-wrap" },
          h(
            "table",
            { class: "grid" },
            h(
              "thead",
              null,
              h(
                "tr",
                null,
                h("th", null, "层"),
                h("th", { class: "r" }, "促销店"),
                h("th", { class: "r" }, "对照店"),
                h("th", { class: "r" }, "促销均值"),
                h("th", { class: "r" }, "对照均值"),
                h("th", { class: "r" }, "层内差异"),
                h("th", { class: "r" }, "权重")
              )
            ),
            h(
              "tbody",
              null,
              ...payload.strata.map((s) =>
                h(
                  "tr",
                  null,
                  h("td", null, s.stratum),
                  h("td", { class: "r" }, s.promo_stores),
                  h("td", { class: "r" }, s.control_stores),
                  h("td", { class: "r" }, fmt.num(s.promo_mean, 2)),
                  h("td", { class: "r" }, fmt.num(s.control_mean, 2)),
                  h("td", { class: "r", style: "color:" + (s.difference >= 0 ? "var(--promo)" : "var(--danger)") }, fmt.signed(s.difference, 2)),
                  h("td", { class: "r" }, fmt.pct(s.weight, 1))
                )
              )
            )
          )
        )
      );
    }

    if (payload.pairs && payload.pairs.length) {
      wrap.append(
        h(
          "div",
          { class: "subhead" },
          h("h3", null, "匹配对"),
          h("span", { class: "hint" }, `${payload.pairs.length} 对；距离为倾向得分 logit 差 / SD`)
        )
      );
      wrap.append(
        h(
          "div",
          { class: "table-wrap" },
          h(
            "table",
            { class: "grid" },
            h(
              "thead",
              null,
              h(
                "tr",
                null,
                h("th", null, "促销门店"),
                h("th", null, "对照门店"),
                h("th", { class: "r" }, "促销结果"),
                h("th", { class: "r" }, "对照结果"),
                h("th", { class: "r" }, "差异"),
                h("th", { class: "r" }, "距离 SD")
              )
            ),
            h(
              "tbody",
              null,
              ...payload.pairs.map((p) =>
                h(
                  "tr",
                  null,
                  h("td", null, `${p.promo_store_name}`, h("span", { class: "mono", style: "color:var(--muted)" }, ` ${p.promo_store_id}`)),
                  h("td", null, `${p.control_store_name}`, h("span", { class: "mono", style: "color:var(--muted)" }, ` ${p.control_store_id}`)),
                  h("td", { class: "r" }, fmt.num(p.promo_outcome, 2)),
                  h("td", { class: "r" }, fmt.num(p.control_outcome, 2)),
                  h("td", { class: "r" }, fmt.signed(p.promo_outcome - p.control_outcome, 2)),
                  h("td", { class: "r" }, fmt.num(p.distance_sd, 3))
                )
              )
            )
          )
        )
      );
    }
    return wrap;
  }

  function renderSupportCards(payload) {
    const support = payload.support || {};
    const cards = [];
    if (support.overlap_range) {
      cards.push(
        h(
          "div",
          { class: "kpi" },
          h("div", { class: "k-label" }, "倾向得分重叠区间"),
          h("div", { class: "k-value", style: "font-size:17px" }, `${fmt.num(support.overlap_range[0], 3)} ~ ${fmt.num(support.overlap_range[1], 3)}`),
          h(
            "div",
            { class: "k-sub" },
            `促销 ${fmt.num(support.promo_range[0], 3)}~${fmt.num(support.promo_range[1], 3)}；对照 ${fmt.num(support.control_range[0], 3)}~${fmt.num(support.control_range[1], 3)}`
          )
        ),
        h(
          "div",
          { class: "kpi" },
          h("div", { class: "k-label" }, "区间外门店"),
          h("div", { class: "k-value", style: "font-size:22px" }, String(support.outside_overlap_count ?? "—")),
          h("div", { class: "k-sub" }, "不具备可比对照，跨区间外推不成立")
        ),
        h(
          "div",
          { class: "kpi" },
          h("div", { class: "k-label" }, "卡钳宽度"),
          h("div", { class: "k-value", style: "font-size:22px" }, `${fmt.num(support.caliper_sd, 2)} SD`),
          h("div", { class: "k-sub" }, `未匹配促销门店 ${support.unmatched_treated ?? "—"} 家`)
        )
      );
    } else if (support.strata_total) {
      cards.push(
        h(
          "div",
          { class: "kpi" },
          h("div", { class: "k-label" }, "可使用层"),
          h("div", { class: "k-value", style: "font-size:22px" }, `${support.strata_used} / ${support.strata_total}`),
          h("div", { class: "k-sub" }, `${support.strata_dropped} 个层只有一组，整层被排除`)
        ),
        h(
          "div",
          { class: "kpi" },
          h("div", { class: "k-label" }, "覆盖会员"),
          h("div", { class: "k-value", style: "font-size:22px" }, fmt.pct(support.members_used / support.members_total, 1)),
          h("div", { class: "k-sub" }, `${fmt.num(support.members_used)} / ${fmt.num(support.members_total)} 会员`)
        )
      );
    } else {
      cards.push(
        h(
          "div",
          { class: "kpi" },
          h("div", { class: "k-label" }, "未做可比性调整"),
          h("div", { class: "k-value", style: "font-size:18px" }, "全部合格门店"),
          h("div", { class: "k-sub" }, "原始差异不区分选择偏差；请参考调整后方法")
        )
      );
    }
    return cards;
  }

  function renderSmdChart(balance) {
    const width = 720;
    const rowH = 24;
    const height = balance.length * rowH + 46;
    const margin = { left: 150, right: 40, top: 22, bottom: 22 };
    const innerW = width - margin.left - margin.right;
    const maxAbs = Math.max(0.2, ...balance.map((b) => Math.max(Math.abs(b.smd_unadjusted), Math.abs(b.smd_adjusted)))) * 1.1;
    const x = (v) => margin.left + ((v + maxAbs) / (2 * maxAbs)) * innerW;
    const svg = svgEl("svg", { class: "chart", viewBox: `0 0 ${width} ${height}`, role: "img", "aria-label": "调整前后标准化均值差" });
    svg.append(
      svgEl("line", { x1: margin.left, x2: width - margin.right, y1: 14, y2: 14, stroke: COLORS.grid }),
      svgEl("text", { x: x(-0.1), y: 11, "text-anchor": "middle", "font-size": 10, fill: COLORS.muted }, "-0.1"),
      svgEl("text", { x: x(0), y: 11, "text-anchor": "middle", "font-size": 10, fill: COLORS.muted }, "0"),
      svgEl("text", { x: x(0.1), y: 11, "text-anchor": "middle", "font-size": 10, fill: COLORS.muted }, "0.1"),
      svgEl("line", { x1: x(-0.1), x2: x(-0.1), y1: 6, y2: height - 18, stroke: "#efe7dc", "stroke-dasharray": "2 3" }),
      svgEl("line", { x1: x(0.1), x2: x(0.1), y1: 6, y2: height - 18, stroke: "#efe7dc", "stroke-dasharray": "2 3" }),
      svgEl("line", { x1: margin.left, x2: width - margin.right, y1: height - 18, y2: height - 18, stroke: COLORS.grid })
    );
    balance.forEach((row, i) => {
      const y = 30 + i * rowH;
      svg.append(
        svgEl("text", { x: 8, y: y + 4, "font-size": 10.5, fill: COLORS.ink }, row.label.length > 15 ? row.label.slice(0, 15) + "…" : row.label),
        svgEl("line", {
          x1: x(row.smd_unadjusted),
          x2: x(row.smd_adjusted),
          y1: y,
          y2: y,
          stroke: COLORS.grid,
          "stroke-width": 3,
        }),
        svgEl("circle", { cx: x(row.smd_unadjusted), cy: y, r: 3.4, fill: COLORS.raw }, svgEl("title", null, `调整前 ${row.smd_unadjusted}`)),
        svgEl("circle", { cx: x(row.smd_adjusted), cy: y, r: 3.4, fill: COLORS.est }, svgEl("title", null, `调整后 ${row.smd_adjusted}`))
      );
    });
    const holder = h("div", null, svg);
    holder.append(
      h(
        "div",
        { class: "legend" },
        h("span", { class: "it" }, h("span", { class: "sw", style: `background:${COLORS.raw}` }), "调整前"),
        h("span", { class: "it" }, h("span", { class: "sw", style: `background:${COLORS.est}` }), "调整后（当前方法）"),
        h("span", { class: "it" }, "虚线为 ±0.1 参考阈值")
      )
    );
    return holder;
  }

  // --------------------------------------------------------------- stores
  function renderStoresTab(payload) {
    const wrap = h("div");
    const filter = PL.state.storeFilter;
    const header = h(
      "div",
      { class: "actions", style: "margin-bottom:10px" },
      h("input", {
        type: "text",
        placeholder: "搜索门店名或 ID",
        value: filter.q,
        style: "max-width:220px",
        oninput: (e) => {
          filter.q = e.target.value;
          rerenderStoreTable(wrap, payload);
        },
      }),
      h(
        "select",
        {
          style: "max-width:130px",
          onchange: (e) => {
            filter.group = e.target.value;
            rerenderStoreTable(wrap, payload);
          },
        },
        ...[["all", "全部门店"], ["promo", "仅促销"], ["control", "仅对照"], ["excluded", "仅未进入分析"]].map(([v, l]) =>
          h("option", { value: v, selected: filter.group === v }, l)
        )
      ),
      h(
        "label",
        { class: "check" },
        h("input", {
          type: "checkbox",
          checked: filter.onlyEligible,
          onchange: (e) => {
            filter.onlyEligible = e.target.checked;
            rerenderStoreTable(wrap, payload);
          },
        }),
        "只看进入比较的门店"
      ),
      h("span", { class: "hint", style: "color:var(--muted);font-size:11.5px" }, "点门店行下钻；结果列单位为元/会员/28 天")
    );
    wrap.append(header);
    const tableHolder = h("div");
    wrap.append(tableHolder);
    rerenderStoreTable(tableHolder, payload);
    return wrap;
  }

  function rerenderStoreTable(holder, payload) {
    const filter = PL.state.storeFilter;
    const q = filter.q.trim().toLowerCase();
    let rows = payload.stores;
    if (q) rows = rows.filter((r) => r.store_id.toLowerCase().includes(q) || r.store_name.toLowerCase().includes(q));
    if (filter.group === "promo" || filter.group === "control") rows = rows.filter((r) => r.group === filter.group);
    if (filter.group === "excluded") rows = rows.filter((r) => !r.eligible);
    if (filter.onlyEligible) rows = rows.filter((r) => r.eligible);
    holder.innerHTML = "";
    if (!rows.length) {
      holder.append(emptyBox("没有匹配的门店", "调整搜索或分组筛选。"));
      return;
    }
    const weightLabel = payload.headline.method_algorithm === "matched" ? "匹配权重" : payload.headline.method_algorithm === "stratified" ? "层权重" : "分析权重";
    const table = h(
      "table",
      { class: "grid" },
      h(
        "thead",
        null,
        h(
          "tr",
          null,
          h("th", null, "门店"),
          h("th", null, "组"),
          h("th", null, "区域 / 业态"),
          h("th", { class: "r" }, "会员"),
          h("th", { class: "r" }, "期前"),
          h("th", { class: "r" }, "活动期"),
          h("th", { class: "r" }, "变化"),
          h("th", { class: "r" }, weightLabel),
          h("th", { class: "r" }, "归因占比"),
          h("th", null, "状态")
        )
      ),
      h(
        "tbody",
        null,
        ...rows.map((row) =>
          h(
            "tr",
            {
              class: "clickable" + (row.eligible ? "" : " dim"),
              onclick: () => views.openStore(row.store_id),
              title: "点击查看周度明细",
            },
            h(
              "td",
              null,
              h("div", { style: "font-weight:600" }, row.store_name),
              h("div", { class: "mono", style: "color:var(--muted)" }, row.store_id)
            ),
            h("td", null, groupTag(row.group)),
            h("td", null, `${row.region} / ${row.format}`),
            h("td", { class: "r" }, fmt.num(row.members)),
            h("td", { class: "r" }, fmt.num(row.pre_rate_asof, 2)),
            h("td", { class: "r" }, fmt.num(row.campaign_rate_asof, 2)),
            h(
              "td",
              { class: "r", style: "font-weight:600;color:" + ((row.outcome_asof ?? 0) >= 0 ? "var(--promo)" : "var(--danger)") },
              fmt.signed(row.outcome_asof, 2)
            ),
            h("td", { class: "r" }, row.weight ? fmt.num(row.weight, 3) : "0"),
            h("td", { class: "r" }, fmt.pct(row.attributed_campaign_share, 1)),
            h(
              "td",
              null,
              row.eligible
                ? row.weight > 0
                  ? h("span", { class: "tag muted" }, "进入比较")
                  : h("span", { class: "tag est" }, "方法排除")
                : h("span", { class: "tag bad", title: row.exclusion_reason }, "数据排除")
            )
          )
        )
      )
    );
    holder.append(h("div", { class: "table-wrap" }, table));
    const counted = rows.filter((r) => r.weight > 0).length;
    holder.append(
      h(
        "div",
        { class: "hint", style: "margin-top:6px;color:var(--muted);font-size:11.5px" },
        `显示 ${rows.length} 家，其中权重 > 0 的 ${counted} 家。窗口期不足或订单太少的门店会在“排除样本”中说明原因。`
      )
    );
  }

  // -------------------------------------------------------------- coverage
  function renderCoverageTab(payload) {
    const wrap = h("div");
    const counts = payload.exclusions.counts;
    const rowLevel = payload.exclusions.row_level;
    wrap.append(
      h(
        "div",
        { class: "stat-strip" },
        h("div", { class: "s" }, h("div", { class: "l" }, "门店总数"), h("div", { class: "v" }, fmt.num(counts.stores_total))),
        h("div", { class: "s" }, h("div", { class: "l" }, "数据合格"), h("div", { class: "v" }, fmt.num(counts.stores_eligible))),
        h("div", { class: "s" }, h("div", { class: "l" }, "数据不足"), h("div", { class: "v" }, fmt.num(counts.stores_excluded))),
        h("div", { class: "s" }, h("div", { class: "l" }, "方法排除"), h("div", { class: "v" }, fmt.num(payload.exclusions.method_level.length))),
        h("div", { class: "s" }, h("div", { class: "l" }, "最终进入比较"), h("div", { class: "v" }, fmt.num(payload.headline.n_stores_promo + payload.headline.n_stores_control)))
      )
    );
    wrap.append(
      h(
        "div",
        { class: "subhead" },
        h("h3", null, "订单级排除"),
        h("span", { class: "hint" }, "明细可在“门店明细”逐店查看")
      )
    );
    const rowItems = [
      ["订单总数（快照内）", rowLevel.orders_total],
      ["状态不在规则范围", rowLevel.orders_excluded_status],
      ["渠道不在规则范围", rowLevel.orders_excluded_channel],
      ["金额不符合要求", rowLevel.orders_excluded_gross],
      ["门店主数据缺失", rowLevel.orders_unknown_store],
      ["退款行总数", rowLevel.refunds_total],
      ["退款指向未知订单", rowLevel.refunds_orphan_order],
      ["退款金额超过订单净额（已截断）", rowLevel.refunds_capped],
      ["曝光行总数", rowLevel.exposure_total],
      ["曝光类型不在规则范围", rowLevel.exposure_excluded_kind],
      ["曝光渠道不在规则范围", rowLevel.exposure_excluded_channel],
    ];
    wrap.append(
      h(
        "div",
        { class: "kv", style: "max-width:460px;background:var(--panel-soft);border:1px solid var(--line);border-radius:8px;padding:10px 12px" },
        ...rowItems.flatMap(([label, value]) => [h("div", { class: "k" }, label), h("div", { class: "v" }, fmt.num(value))])
      )
    );

    const methodRows = payload.exclusions.method_level || [];
    wrap.append(
      h(
        "div",
        { class: "subhead" },
        h("h3", null, "方法层面被排除的门店"),
        h("span", { class: "hint" }, methodRows.length ? "这些门店数据合格，但当前方法无法给出可比对照" : "当前方法没有额外排除门店")
      )
    );
    if (methodRows.length) {
      wrap.append(
        h(
          "div",
          { class: "table-wrap short" },
          h(
            "table",
            { class: "grid" },
            h("thead", null, h("tr", null, h("th", null, "门店"), h("th", null, "组"), h("th", null, "原因"), h("th", { class: "r" }, "倾向得分"))),
            h(
              "tbody",
              null,
              ...methodRows.map((row) =>
                h(
                  "tr",
                  null,
                  h("td", null, `${row.store_name || ""} `, h("span", { class: "mono", style: "color:var(--muted)" }, row.store_id)),
                  h("td", null, groupTag(row.group)),
                  h("td", null, row.reason),
                  h("td", { class: "r" }, row.propensity === undefined ? "—" : fmt.num(row.propensity, 4))
                )
              )
            )
          )
        )
      );
    }

    const dataExcluded = (payload.stores || []).filter((s) => !s.eligible || (s.exclusion_reason && s.exclusion_reason.indexOf("页面") >= 0));
    wrap.append(
      h(
        "div",
        { class: "subhead" },
        h("h3", null, "数据或筛选原因被排除的门店"),
        h("span", { class: "hint" }, `${dataExcluded.length} 家`)
      )
    );
    if (dataExcluded.length) {
      wrap.append(
        h(
          "div",
          { class: "table-wrap short" },
          h(
            "table",
            { class: "grid" },
            h("thead", null, h("tr", null, h("th", null, "门店"), h("th", null, "组"), h("th", { class: "r" }, "期前订单"), h("th", { class: "r" }, "活动订单"), h("th", null, "原因"))),
            h(
              "tbody",
              null,
              ...dataExcluded.map((row) =>
                h(
                  "tr",
                  null,
                  h("td", null, `${row.store_name} `, h("span", { class: "mono", style: "color:var(--muted)" }, row.store_id)),
                  h("td", null, groupTag(row.group)),
                  h("td", { class: "r" }, fmt.num(row.pre_orders)),
                  h("td", { class: "r" }, fmt.num(row.campaign_orders)),
                  h("td", null, row.exclusion_reason || "—")
                )
              )
            )
          )
        )
      );
    } else {
      wrap.append(h("p", { style: "color:var(--muted);font-size:12px" }, "当前条件下没有被数据门槛或筛选排除的门店。"));
    }

    // common support list
    const support = payload.support || {};
    if (support.outside_overlap_stores && support.outside_overlap_stores.length) {
      wrap.append(
        h("div", { class: "subhead" }, h("h3", null, "落在共同支持区间外的门店")),
        h(
          "p",
          { style: "color:var(--muted);font-size:12px" },
          "这些门店的倾向得分不在两组重叠区间内；匹配不会用它们，也不应把结果外推到它们身上。"
        ),
        h(
          "div",
          { class: "multi", style: "gap:5px" },
          ...support.outside_overlap_stores.map((sid) => h("span", { class: "tag muted mono" }, sid))
        )
      );
    }
    return wrap;
  }

  // ----------------------------------------------------------- sensitivity
  function renderSensitivityTab(payload) {
    const wrap = h("div");
    wrap.append(
      warning(
        "敏感性分析改变一个选择再重算同一定义下的估计。它用来说明结论对选择的依赖，<strong>不是置信区间</strong>，也不检验未观测混杂。"
      )
    );
    wrap.append(
      h(
        "div",
        { class: "subhead" },
        h("h3", null, "情景对比"),
        h("span", { class: "hint" }, "主估计旁边为差值")
      )
    );
    wrap.append(renderSensitivityChart(payload, { compact: false }));
    wrap.append(
      h(
        "div",
        { class: "table-wrap", style: "margin-top:12px" },
        h(
          "table",
          { class: "grid" },
          h(
            "thead",
            null,
            h(
              "tr",
              null,
              h("th", null, "情景"),
              h("th", null, "说明"),
              h("th", { class: "r" }, "估计"),
              h("th", { class: "r" }, "相对主估计"),
              h("th", { class: "r" }, "促销店"),
              h("th", { class: "r" }, "对照店")
            )
          ),
          h(
            "tbody",
            null,
            ...payload.sensitivity.map((row) =>
              h(
                "tr",
                { class: row.is_primary ? "" : "" },
                h("td", null, row.name, row.is_primary ? h("span", { class: "tag est", style: "margin-left:6px" }, "主估计") : null),
                h("td", { style: "color:var(--muted)" }, row.detail),
                h("td", { class: "r", style: "font-weight:600" }, fmt.signed(row.estimate)),
                h("td", { class: "r" }, fmt.signed(row.delta_vs_primary)),
                h("td", { class: "r" }, fmt.num(row.n_promo)),
                h("td", { class: "r" }, fmt.num(row.n_control))
              )
            )
          )
        )
      )
    );
    wrap.append(
      h("div", { class: "subhead" }, h("h3", null, "退款数据质量")),
      h(
        "div",
        { class: "kv", style: "max-width:560px;background:var(--panel-soft);border:1px solid var(--line);border-radius:8px;padding:10px 12px" },
        h("div", { class: "k" }, "快照中的退款行"),
        h("div", { class: "v" }, fmt.num(payload.refund_quality.refunds_in_snapshot)),
        h("div", { class: "k" }, "截止日之后到账"),
        h("div", { class: "v" }, `${fmt.num(payload.refund_quality.refunds_after_cutoff_in_snapshot)} 笔（${fmt.num(payload.refund_quality.refunds_after_cutoff_cents / 100, 0)} 元）`),
        h("div", { class: "k" }, "当前口径"),
        h("div", { class: "v" }, fmt.refundMode(payload.refund_quality.refund_mode)),
        h("div", { class: "k" }, "退款截止"),
        h("div", { class: "v mono" }, payload.refund_quality.refund_cutoff)
      ),
      h("p", { style: "color:var(--muted);font-size:11.5px;margin-top:8px" }, payload.refund_quality.note)
    );
    return wrap;
  }

  function renderSensitivityChart(payload, options) {
    const rows = payload.sensitivity;
    const width = 720;
    const rowH = options.compact ? 30 : 34;
    const height = rows.length * rowH + 56;
    const margin = { left: 168, right: 54, top: 26, bottom: 26 };
    const innerW = width - margin.left - margin.right;
    const all = rows.map((r) => r.estimate).concat([payload.headline.raw_difference, payload.headline.adjusted_estimate]);
    const [lo, hi] = niceExtent(all, 0.2);
    const x = (v) => margin.left + ((v - lo) / (hi - lo || 1)) * innerW;
    const svg = svgEl("svg", { class: "chart", viewBox: `0 0 ${width} ${height}`, role: "img", "aria-label": "敏感性估计对比" });
    for (const tick of ticks(lo, hi, 5)) {
      svg.append(
        svgEl("line", { x1: x(tick), x2: x(tick), y1: 18, y2: height - 22, stroke: COLORS.grid }),
        svgEl("text", { x: x(tick), y: height - 8, "text-anchor": "middle", "font-size": 10, fill: COLORS.muted }, fmt.num(tick, 1))
      );
    }
    svg.append(
      svgEl("line", {
        x1: x(payload.headline.adjusted_estimate),
        x2: x(payload.headline.adjusted_estimate),
        y1: 14,
        y2: height - 22,
        stroke: COLORS.est,
        "stroke-width": 1.4,
        "stroke-dasharray": "4 3",
      }),
      svgEl("text", { x: x(payload.headline.adjusted_estimate), y: 11, "text-anchor": "middle", "font-size": 10, fill: COLORS.est }, "主估计")
    );
    rows.forEach((row, i) => {
      const y = 32 + i * rowH;
      const isRaw = row.name === "退款处理方式改为另一种";
      const color = row.is_primary ? COLORS.est : isRaw ? COLORS.raw : COLORS.promo;
      svg.append(
        svgEl("text", { x: 8, y: y + 4, "font-size": 10.5, fill: COLORS.ink }, row.name.length > 19 ? row.name.slice(0, 19) + "…" : row.name),
        svgEl("line", { x1: margin.left - 6, x2: x(row.estimate), y1: y, y2: y, stroke: "#e9e6de", "stroke-width": 5, "stroke-linecap": "round" }),
        svgEl("circle", { cx: x(row.estimate), cy: y, r: 4.6, fill: color }, svgEl("title", null, `${row.name}: ${row.estimate}（${row.detail}）`)),
        svgEl("text", { x: x(row.estimate) + 9, y: y + 4, "font-size": 10.5, fill: COLORS.ink }, fmt.signed(row.estimate))
      );
    });
    const holder = h("div", null, svg);
    holder.append(
      h(
        "div",
        { class: "legend" },
        h("span", { class: "it" }, h("span", { class: "sw", style: `background:${COLORS.est}` }), "当前方法主估计"),
        h("span", { class: "it" }, h("span", { class: "sw", style: `background:${COLORS.promo}` }), "其他情景"),
        h("span", { class: "it" }, "单位：元/会员/28 天")
      )
    );
    return holder;
  }

  // ----------------------------------------------------------- attribution
  function renderAttributionTab(report) {
    const payload = report.payload;
    const attribution = payload.attribution || {};
    const spec = report.spec;
    const wrap = h("div");
    const rows = attribution.sample_rows || [];
    const coverage =
      attribution.campaign_eligible_orders > 0
        ? attribution.campaign_attributed_orders / attribution.campaign_eligible_orders
        : null;
    wrap.append(
      warning(
        "曝光归属是把订单与订单发生前的曝光按规则连起来。它回答“这笔订单能不能算作这次促销触达之后发生的”，<strong>不区分自然流量与促销增量</strong>，所以归因占比不是提升幅度。"
      )
    );
    wrap.append(
      h(
        "div",
        { class: "stat-strip" },
        h("div", { class: "s" }, h("div", { class: "l" }, "规则版本"), h("div", { class: "v mono" }, attribution.rule_version || report.rule_version)),
        h("div", { class: "s" }, h("div", { class: "l" }, "回看窗口"), h("div", { class: "v" }, `${spec.rule.lookback_hours} 小时`)),
        h("div", { class: "s" }, h("div", { class: "l" }, "全部门店活动期订单（行级规则后）"), h("div", { class: "v" }, fmt.num(attribution.campaign_eligible_orders))),
        h("div", { class: "s" }, h("div", { class: "l" }, "其中可归因订单"), h("div", { class: "v" }, fmt.num(attribution.campaign_attributed_orders))),
        h("div", { class: "s" }, h("div", { class: "l" }, "订单归因覆盖率"), h("div", { class: "v" }, fmt.pct(coverage, 1)))
      )
    );
    wrap.append(
      h(
        "p",
        { style: "color:var(--muted);font-size:12px;margin:10px 0 0" },
        attribution.note || "按订单向前回看窗口内最近的合格曝光。",
        " 这里的订单数按订单行级规则统计（状态、渠道、金额、时间窗），包含之后因订单门槛或页面筛选而未进入方法的门店；它描述归属口径的覆盖，不对应任何增量。"
      )
    );
    wrap.append(
      h(
        "div",
        { class: "stat-strip", style: "margin-top:10px" },
        h("div", { class: "s" }, h("div", { class: "l" }, "计入的订单状态"), h("div", { class: "v", style: "font-size:13px" }, spec.rule.order_statuses.join("、"))),
        h("div", { class: "s" }, h("div", { class: "l" }, "计入渠道"), h("div", { class: "v", style: "font-size:13px" }, spec.rule.channel_scope.map((c) => PL.channelLabel(c)).join("、"))),
        h("div", { class: "s" }, h("div", { class: "l" }, "曝光类型"), h("div", { class: "v", style: "font-size:13px" }, spec.rule.exposure_kinds.map((k) => (k === "campaign" ? "促销曝光" : "常规曝光")).join("、"))),
        h("div", { class: "s" }, h("div", { class: "l" }, "同门店曝光"), h("div", { class: "v", style: "font-size:13px" }, spec.rule.require_same_store ? "要求" : "不要求")),
        h("div", { class: "s" }, h("div", { class: "l" }, "只看订单前曝光"), h("div", { class: "v", style: "font-size:13px" }, spec.rule.post_exposure_only ? "是" : "否"))
      )
    );
    if (rows.length) {
      wrap.append(
        h(
          "div",
          { class: "subhead" },
          h("h3", null, "归属抽样"),
          h("span", { class: "hint" }, `${rows.length} 笔订单样本（命中与未命中混排，按订单号截取；未按门店资格筛选）`)
        )
      );
      wrap.append(
        h(
          "div",
          { class: "table-wrap" },
          h(
            "table",
            { class: "grid" },
            h(
              "thead",
              null,
              h(
                "tr",
                null,
                h("th", null, "订单"),
                h("th", null, "门店 / 组"),
                h("th", null, "下单时间"),
                h("th", { class: "r" }, "净额（元）"),
                h("th", null, "归因曝光"),
                h("th", null, "曝光时间"),
                h("th", { class: "r" }, "候选数"),
                h("th", null, "窗口")
              )
            ),
            h(
              "tbody",
              null,
              ...rows.map((row) =>
                h(
                  "tr",
                  { class: row.attributed ? "" : "dim" },
                  h("td", { class: "mono" }, row.order_id),
                  h(
                    "td",
                    null,
                    `${row.store_name || row.store_id} `,
                    row.store_group ? h("span", { class: "tag " + row.store_group }, fmt.group(row.store_group)) : null
                  ),
                  h("td", { class: "mono" }, fmt.ts(row.ordered_at)),
                  h("td", { class: "r" }, fmt.num(row.net_cents / 100, 2)),
                  h(
                    "td",
                    null,
                    row.attributed
                      ? `${PL.channelLabel(row.exposure_channel)} · ${row.exposure_kind === "campaign" ? "促销曝光" : "常规曝光"}`
                      : h("span", { class: "tag muted" }, "未命中")
                  ),
                  h("td", { class: "mono" }, row.exposure_at ? fmt.ts(row.exposure_at) : "—"),
                  h("td", { class: "r" }, row.attributed ? String(row.candidate_count) : "—"),
                  h("td", null, row.window === "campaign" ? h("span", { class: "tag promo" }, "活动期") : h("span", { class: "tag muted" }, "期前"))
                )
              )
            )
          )
        )
      );
    } else {
      wrap.append(emptyBox("没有归属抽样", "该报告没有返回可展示的曝光归属样本。"));
    }
    return wrap;
  }

  // -------------------------------------------------------------- evidence
  function renderEvidenceTab(report) {
    const payload = report.payload;
    const wrap = h("div");
    const spec = report.spec;
    const inputRows = [
      ["报告 ID", report.report_id],
      ["组合 / 版本", `${report.combo_id} · v${report.version_number}`],
      ["快照", report.snapshot_hash],
      ["快照创建", payload.inputs.snapshot_created_at],
      ["最大接入序号", String(payload.inputs.max_ingest_seq)],
      ["归因规则版本", report.rule_version],
      ["观察窗版本", report.cohort_version],
      ["方法版本", report.method_version],
      ["方法参数", JSON.stringify(spec.method_params)],
      ["订单门槛", String(spec.threshold)],
      ["筛选", JSON.stringify(spec.filters)],
      ["输入摘要", report.inputs_digest],
      ["结果摘要", report.payload_digest],
      ["引擎版本", report.engine_version],
    ];
    wrap.append(
      h(
        "div",
        { class: "compare-grid" },
        h(
          "div",
          { class: "card" },
          h("h3", null, "本报告绑定的输入"),
          h("div", { class: "kv" }, ...inputRows.flatMap(([k, v]) => [h("div", { class: "k" }, k), h("div", { class: "v mono" }, v)]))
        ),
        h(
          "div",
          { class: "card" },
          h("h3", null, "规则与观察窗取值"),
          h(
            "div",
            { class: "kv" },
            h("div", { class: "k" }, "回看窗口"),
            h("div", { class: "v" }, `${spec.rule.lookback_hours} 小时`),
            h("div", { class: "k" }, "订单状态"),
            h("div", { class: "v" }, spec.rule.order_statuses.join("、")),
            h("div", { class: "k" }, "渠道"),
            h("div", { class: "v" }, spec.rule.channel_scope.join("、")),
            h("div", { class: "k" }, "曝光类型"),
            h("div", { class: "v" }, spec.rule.exposure_kinds.join("、")),
            h("div", { class: "k" }, "同门店曝光"),
            h("div", { class: "v" }, spec.rule.require_same_store ? "是" : "否"),
            h("div", { class: "k" }, "活动窗口"),
            h("div", { class: "v" }, `${spec.cohort.campaign_start} ~ ${spec.cohort.campaign_end}`),
            h("div", { class: "k" }, "期前窗口"),
            h("div", { class: "v" }, `${spec.cohort.pre_start} ~ ${spec.cohort.pre_end}`),
            h("div", { class: "k" }, "退款口径"),
            h("div", { class: "v" }, fmt.refundMode(spec.cohort.refund_mode)),
            h("div", { class: "k" }, "退款截止"),
            h("div", { class: "v mono" }, spec.cohort.refund_cutoff)
          )
        )
      )
    );

    wrap.append(
      h("div", { class: "subhead" }, h("h3", null, "快照内容"), h("span", { class: "hint" }, "报告只读这份冻结数据，后续接入不会改写它")),
      h(
        "div",
        { class: "kv", style: "max-width:560px;background:var(--panel-soft);border:1px solid var(--line);border-radius:8px;padding:10px 12px" },
        ...Object.entries(payload.inputs.snapshot_row_counts).flatMap(([table, count]) => [
          h("div", { class: "k" }, table),
          h("div", { class: "v" }, `${fmt.num(count)} 行 · ${fmt.shortHash(payload.inputs.snapshot_table_digests[table], 12)}`),
        ])
      )
    );

    wrap.append(
      h("div", { class: "subhead" }, h("h3", null, "运行与状态变更")),
      h(
        "div",
        { class: "table-wrap short" },
        h(
          "table",
          { class: "grid" },
          h("thead", null, h("tr", null, h("th", null, "时间"), h("th", null, "事件"), h("th", null, "说明"))),
          h(
            "tbody",
            null,
            ...report.events.map((e) => h("tr", null, h("td", { class: "mono" }, fmt.ts(e.at)), h("td", null, eventLabel(e.kind)), h("td", { style: "color:var(--muted)" }, e.detail)))
          )
        )
      )
    );
    wrap.append(
      h("div", { class: "subhead" }, h("h3", null, "导出依据")),
      h(
        "div",
        { class: "actions" },
        h("a", { class: "btn", href: `/api/reports/${report.report_id}/evidence`, download: "" }, "完整依据 JSON"),
        h("a", { class: "btn", href: `/api/reports/${report.report_id}/stores.csv`, download: "" }, "门店级 CSV"),
        h("a", { class: "btn", href: `/api/reports/${report.report_id}/methodology.md`, download: "" }, "方法说明 Markdown")
      ),
      h(
        "p",
        { style: "color:var(--muted);font-size:11.5px;margin-top:8px" },
        "依据文件包含冻结输入、规则/观察窗/方法版本与参数、排除样本、共同支持、敏感性、事件链与复现请求体。"
      )
    );
    return wrap;
  }

  function eventLabel(kind) {
    return {
      enqueued: "入队",
      started: "开始计算",
      finalized: "完成",
      failed: "失败",
      superseded: "被取代",
    }[kind] || kind;
  }

  // ---------------------------------------------------------------- drawer
  views.openStore = async function (storeId) {
    const report = PL.state.report;
    if (!report) return;
    const drawer = $("#drawer");
    const body = $("#drawer-body");
    $("#drawer-title").textContent = "门店明细";
    $("#drawer-sub").textContent = "加载中…";
    body.innerHTML = "";
    drawer.classList.add("open");
    drawer.setAttribute("aria-hidden", "false");
    $("#drawer-scrim").classList.add("open");
    try {
      const detail = await api(`/api/stores/${storeId}?report_id=${report.report_id}`);
      renderDrawer(detail, report);
    } catch (err) {
      body.append(warning(esc(err.message), "warn"));
    }
  };

  views.closeDrawer = function () {
    $("#drawer").classList.remove("open");
    $("#drawer").setAttribute("aria-hidden", "true");
    $("#drawer-scrim").classList.remove("open");
  };

  function renderDrawer(detail, report) {
    const store = detail.store;
    const row = detail.report_row || {};
    $("#drawer-title").textContent = store.store_name;
    $("#drawer-sub").textContent = `${store.store_id} · ${store.region} / ${store.format} · ${fmt.group(store.group)} · 报告 v${report.version_number}`;
    const body = $("#drawer-body");
    body.innerHTML = "";
    body.append(
      h(
        "div",
        { class: "stat-strip" },
        h("div", { class: "s" }, h("div", { class: "l" }, "会员数"), h("div", { class: "v" }, fmt.num(store.members))),
        h("div", { class: "s" }, h("div", { class: "l" }, "面积"), h("div", { class: "v" }, `${fmt.num(store.size_sqm)} m²`)),
        h("div", { class: "s" }, h("div", { class: "l" }, "客流指数"), h("div", { class: "v" }, fmt.num(store.traffic_index, 2))),
        h("div", { class: "s" }, h("div", { class: "l" }, "城市化"), h("div", { class: "v" }, fmt.num(store.urban_score, 2))),
        h("div", { class: "s" }, h("div", { class: "l" }, "会员销售占比"), h("div", { class: "v" }, fmt.pct(store.member_share, 1)))
      )
    );
    if (row && row.exclusion_reason) {
      body.append(warning(`该门店未进入当前比较：<strong>${esc(row.exclusion_reason)}</strong>`, "warn"));
    } else if (row && row.weight !== undefined) {
      body.append(
        warning(
          `当前方法中的权重：<strong>${fmt.num(row.weight, 4)}</strong>` +
            (row.matched_to ? `；匹配到对照门店 <span class="mono">${esc(row.matched_to)}</span>` : "") +
            (row.weight === 0 ? "（权重为 0，未参与估计）" : ""),
          row.weight === 0 ? "warn" : ""
        )
      );
    }
    if (row && row.eligible) {
      body.append(
        h(
          "div",
          { class: "actions", style: "margin:10px 0" },
          h(
            "button",
            {
              class: "btn small danger",
              onclick: () => {
                if (!PL.state.excludeIds.includes(store.store_id)) {
                  PL.state.excludeIds.push(store.store_id);
                  PL.state.draft.filters.exclude_store_ids = PL.state.excludeIds.slice();
                  PL.renderControls();
                }
                PL.toast(`已将 ${store.store_id} 加入下次重算的排除列表`);
              },
            },
            "加入排除列表"
          ),
          h(
            "button",
            {
              class: "btn small",
              onclick: async () => {
                try {
                  await navigator.clipboard.writeText(store.store_id);
                  PL.toast("门店 ID 已复制");
                } catch (err) {
                  PL.toast("复制失败：" + err.message, true);
                }
              },
            },
            "复制门店 ID"
          )
        )
      );
    }

    body.append(h("div", { class: "subhead" }, h("h3", null, "周度净销售"), h("span", { class: "hint" }, "元；活动期含退款截止日前到账的退款抵扣")));
    body.append(renderWeeklyChart(detail.series, detail.windows));

    body.append(
      h(
        "div",
        { class: "subhead" },
        h("h3", null, "最近窗口汇总"),
        h("span", { class: "hint" }, `有效订单 ${fmt.num(detail.totals.eligible_orders)}；归因订单 ${fmt.num(detail.totals.attributed_orders)}`)
      )
    );
    body.append(
      h(
        "div",
        { class: "table-wrap short" },
        h(
          "table",
          { class: "grid" },
          h(
            "thead",
            null,
            h(
              "tr",
              null,
              h("th", null, "周起始"),
              h("th", null, "阶段"),
              h("th", { class: "r" }, "净销售（元）"),
              h("th", { class: "r" }, "订单"),
              h("th", { class: "r" }, "归因订单"),
              h("th", { class: "r" }, "归因净销售（元）")
            )
          ),
          h(
            "tbody",
            null,
            ...detail.series.map((w) =>
              h(
                "tr",
                null,
                h("td", { class: "mono" }, w.week),
                h("td", null, w.phase === "campaign" ? h("span", { class: "tag promo" }, "活动期") : h("span", { class: "tag muted" }, "期前")),
                h("td", { class: "r" }, fmt.num(w.net_yuan, 0)),
                h("td", { class: "r" }, fmt.num(w.orders)),
                h("td", { class: "r" }, fmt.num(w.attributed_orders)),
                h("td", { class: "r" }, fmt.num(w.attributed_net_yuan, 0))
              )
            )
          )
        )
      )
    );
    body.append(
      h(
        "p",
        { style: "color:var(--muted);font-size:11.5px;margin-top:10px" },
        "周度数据按订单发生时间归周；退款按退款时间是否早于报告绑定的截止时间决定是否抵扣。"
      )
    );
  }

  function renderWeeklyChart(series, windows) {
    const width = 560;
    const height = 220;
    const margin = { left: 54, right: 12, top: 16, bottom: 34 };
    const innerW = width - margin.left - margin.right;
    const innerH = height - margin.top - margin.bottom;
    const values = series.map((s) => s.net_yuan);
    const [lo, hi] = niceExtent(values.length ? values : [0, 1], 0.1);
    const x = (i) => margin.left + (series.length <= 1 ? innerW / 2 : (i / (series.length - 1)) * innerW);
    const y = (v) => margin.top + (1 - (v - lo) / (hi - lo || 1)) * innerH;
    const svg = svgEl("svg", { class: "chart", viewBox: `0 0 ${width} ${height}`, role: "img", "aria-label": "门店周度净销售" });
    for (const tick of ticks(lo, hi, 4)) {
      svg.append(
        svgEl("line", { x1: margin.left, x2: width - margin.right, y1: y(tick), y2: y(tick), stroke: COLORS.grid }),
        svgEl("text", { x: margin.left - 7, y: y(tick) + 3.5, "text-anchor": "end", "font-size": 10, fill: COLORS.muted }, fmt.num(tick, 0))
      );
    }
    // campaign band
    const campStart = windows.campaign[0];
    const campEnd = windows.campaign[1];
    let startIdx = series.findIndex((s) => s.week >= campStart);
    let endIdx = -1;
    series.forEach((s, i) => {
      if (s.week <= campEnd) endIdx = i;
    });
    if (startIdx >= 0 && endIdx >= startIdx) {
      const x1 = x(startIdx) - (series.length > 1 ? innerW / (series.length - 1) / 2 : 0);
      const x2 = x(endIdx) + (series.length > 1 ? innerW / (series.length - 1) / 2 : 0);
      svg.append(svgEl("rect", { x: x1, y: margin.top, width: Math.max(2, x2 - x1), height: innerH, fill: "rgba(12,107,92,0.06)" }));
      svg.append(
        svgEl("text", { x: (x1 + x2) / 2, y: margin.top + 10, "text-anchor": "middle", "font-size": 9.5, fill: COLORS.promo }, "活动窗口")
      );
    }
    let d = "";
    series.forEach((point, i) => {
      d += (i === 0 ? "M" : "L") + x(i) + "," + y(point.net_yuan) + " ";
    });
    svg.append(svgEl("path", { d: d.trim(), fill: "none", stroke: COLORS.promo, "stroke-width": 2 }));
    series.forEach((point, i) => {
      svg.append(
        svgEl("circle", { cx: x(i), cy: y(point.net_yuan), r: 2.6, fill: COLORS.promo }, svgEl("title", null, `${point.week}：${fmt.num(point.net_yuan)} 元，订单 ${point.orders}`))
      );
    });
    const step = Math.ceil(series.length / 6) || 1;
    series.forEach((point, i) => {
      if (i % step !== 0 && i !== series.length - 1) return;
      svg.append(
        svgEl("text", { x: x(i), y: height - 12, "text-anchor": "middle", "font-size": 9, fill: COLORS.muted }, point.week.slice(5))
      );
    });
    return h("div", null, svg);
  }

  // ================================================================ COMPARE
  views.renderCompare = async function () {
    const root = $("#compare-body");
    if (!root) return;
    let reports = [];
    try {
      const boot = await api("/api/bootstrap");
      PL.state.boot = boot;
      for (const combo of boot.combos) {
        const versions = await api(`/api/combos/${combo.combo_id}/versions`);
        for (const version of versions.versions) {
          reports.push({ ...version, combo_label: combo.label });
        }
      }
    } catch (err) {
      root.innerHTML = "";
      root.append(warning(esc(err.message), "warn"));
      return;
    }
    root.innerHTML = "";
    const finalReports = reports.filter((r) => r.status === "final");
    if (finalReports.length < 2) {
      root.append(emptyBox("至少需要两版已完成报告", "在“分析”页用不同条件或方法各生成一版，然后回来对比。"));
      return;
    }
    if (!PL.state.compare.left || !finalReports.some((r) => r.report_id === PL.state.compare.left)) {
      PL.state.compare.left = finalReports[0].report_id;
    }
    if (!PL.state.compare.right || !finalReports.some((r) => r.report_id === PL.state.compare.right)) {
      PL.state.compare.right = finalReports[0].report_id === finalReports[1].report_id ? finalReports[2]?.report_id || finalReports[1].report_id : finalReports[1].report_id;
    }

    const options = (selected) =>
      finalReports.map((r) =>
        h(
          "option",
          { value: r.report_id, selected: r.report_id === selected },
          `${r.combo_label || r.combo_id} · v${r.version_number} · ${fmt.method(r.spec.method_algorithm)} · ${fmt.refundMode(r.spec.cohort.refund_mode)}`
        )
      );

    const picker = h(
      "div",
      { class: "panel", style: "margin-bottom:14px" },
      h(
        "div",
        { class: "panel-body" },
        h(
          "div",
          { class: "compare-grid" },
          h("div", { class: "field" }, h("label", null, "左侧（基准版本）"), h("select", {
            onchange: (e) => {
              PL.state.compare.left = e.target.value;
              views.renderCompare();
            },
          }, ...options(PL.state.compare.left))),
          h("div", { class: "field" }, h("label", null, "右侧（对照版本）"), h("select", {
            onchange: (e) => {
              PL.state.compare.right = e.target.value;
              views.renderCompare();
            },
          }, ...options(PL.state.compare.right)))
        ),
        h(
          "div",
          { class: "actions" },
          h(
            "button",
            {
              class: "btn primary",
              onclick: async () => {
                try {
                  const result = await api("/api/compare", {
                    method: "POST",
                    body: JSON.stringify({ left: PL.state.compare.left, right: PL.state.compare.right }),
                  });
                  renderCompareResult(root, result);
                } catch (err) {
                  PL.toast(err.message, true);
                }
              },
            },
            "生成对比"
          ),
          h(
            "span",
            { style: "font-size:11.5px;color:var(--muted)" },
            "只对比已完成报告；输入不同之处会逐项列出，结果差异不会脱离输入解释。"
          )
        )
      )
    );
    root.append(picker);

    // recent versions list for context
    const byCombo = {};
    for (const r of reports) (byCombo[r.combo_id] = byCombo[r.combo_id] || []).push(r);
    const listPanel = h("div", { class: "panel" }, h("div", { class: "panel-body" }));
    listPanel.firstChild.append(h("div", { class: "subhead", style: "margin-top:0" }, h("h3", null, "各组合的版本历史")));
    for (const [comboId, items] of Object.entries(byCombo)) {
      const head = h(
        "div",
        { style: "margin:10px 0 6px;font-size:12px;font-weight:650" },
        items[0].combo_label || comboId,
        h("span", { class: "mono", style: "color:var(--muted);font-weight:400" }, ` ${comboId}`)
      );
      listPanel.firstChild.append(head);
      for (const item of items) {
        listPanel.firstChild.append(
          h(
            "div",
            { class: "version-item" + (item.status !== "final" ? " unselectable" : "") },
            h("div", { style: "font-size:11px;color:var(--muted);padding-top:2px" }, `v${item.version_number}`),
            h(
              "div",
              null,
              h("div", { class: "v-title" }, fmt.method(item.spec.method_algorithm), " ", statusBadge(item.status)),
              h(
                "div",
                { class: "v-meta" },
                `${fmt.refundMode(item.spec.cohort.refund_mode)} · 门槛 ${item.spec.threshold} · 快照 ${fmt.shortHash(item.snapshot_hash, 10)} · ${fmt.ts(item.created_at)}`
              )
            ),
            item.status === "final"
              ? h(
                  "div",
                  { style: "display:flex;gap:4px" },
                  h(
                    "button",
                    {
                      class: "btn ghost small",
                      onclick: () => {
                        PL.state.compare.left = item.report_id;
                        views.renderCompare();
                      },
                    },
                    "设为左"
                  ),
                  h(
                    "button",
                    {
                      class: "btn ghost small",
                      onclick: () => {
                        PL.state.compare.right = item.report_id;
                        views.renderCompare();
                      },
                    },
                    "设为右"
                  )
                )
              : null
          )
        );
      }
    }
    root.append(listPanel);
    const resultHolder = h("div", { id: "compare-result", style: "margin-top:14px" });
    root.append(resultHolder);

    try {
      const result = await api("/api/compare", {
        method: "POST",
        body: JSON.stringify({ left: PL.state.compare.left, right: PL.state.compare.right }),
      });
      renderCompareResult(resultHolder, result);
    } catch (err) {
      resultHolder.append(warning(esc(err.message), "warn"));
    }
  };

  function renderCompareResult(root, result) {
    root.innerHTML = "";
    const keys = {
      snapshot_hash: "数据快照",
      rule_version: "归因规则版本",
      cohort_version: "观察窗版本",
      method_version: "方法版本",
      method_params: "方法参数",
      inputs_digest: "输入摘要",
      engine_version: "引擎版本",
      threshold: "订单门槛",
      filters: "筛选",
      refund_mode: "退款口径",
      rule: "归因规则取值",
      cohort: "观察窗取值",
    };
    const panel = h("div", { class: "panel" }, h("div", { class: "panel-body" }));
    panel.firstChild.append(
      h(
        "div",
        { class: "subhead", style: "margin-top:0" },
        h("h3", null, `v${result.left.version_number} → v${result.right.version_number}`),
        h("span", { class: "hint" }, `${result.changed_inputs.length} 项输入不同`)
      )
    );
    const metricLabels = {
      raw_difference: "原始两组差异",
      adjusted_estimate: "调整后估计",
      adjustment_delta: "调整幅度",
      n_stores_promo: "促销门店数",
      n_stores_control: "对照门店数",
    };
    panel.firstChild.append(
      h(
        "div",
        { class: "table-wrap short" },
        h(
          "table",
          { class: "grid" },
          h("thead", null, h("tr", null, h("th", null, "指标"), h("th", { class: "r" }, "左侧"), h("th", { class: "r" }, "右侧"), h("th", { class: "r" }, "差异"))),
          h(
            "tbody",
            null,
            ...Object.entries(result.metrics).map(([key, value]) =>
              h(
                "tr",
                null,
                h("td", null, metricLabels[key] || key),
                h("td", { class: "r" }, value.left === null ? "—" : fmt.num(value.left, key.startsWith("n_") ? 0 : 2)),
                h("td", { class: "r" }, value.right === null ? "—" : fmt.num(value.right, key.startsWith("n_") ? 0 : 2)),
                h(
                  "td",
                  { class: "r", style: "font-weight:600;color:" + ((value.delta ?? 0) >= 0 ? "var(--promo)" : "var(--danger)") },
                  value.delta === null ? "—" : (key.startsWith("n_") ? fmt.signed(value.delta, 0) : fmt.signed(value.delta, 2))
                )
              )
            )
          )
        )
      )
    );
    panel.firstChild.append(
      h(
        "div",
        { class: "subhead" },
        h("h3", null, "输入差异"),
        h("span", { class: "hint" }, "每一版都绑定自己的完整输入，这些差异解释了结果为何可能不同")
      )
    );
    if (!result.changed_inputs.length) {
      panel.firstChild.append(h("p", { style: "color:var(--muted);font-size:12px" }, "两版输入完全一致，应得到相同结果；若结果仍不同，说明有未被冻结的输入，应视为缺陷。"));
    }
    const holder = h("div", { class: "diff-list" });
    for (const key of result.changed_inputs) {
      holder.append(
        h(
          "div",
          { class: "diff-row" },
          h("span", { class: "k" }, keys[key] || key),
          h("span", { class: "a" }, compactValue(result.inputs_left[key])),
          h("span", { style: "color:var(--muted)" }, "→"),
          h("span", { class: "b" }, compactValue(result.inputs_right[key]))
        )
      );
    }
    panel.firstChild.append(holder);
    panel.firstChild.append(
      h(
        "div",
        { class: "stat-strip", style: "margin-top:12px" },
        h("div", { class: "s" }, h("div", { class: "l" }, "左侧进入比较"), h("div", { class: "v" }, `${result.coverage.left_eligible} 家（排除 ${result.coverage.left_excluded}）`)),
        h("div", { class: "s" }, h("div", { class: "l" }, "右侧进入比较"), h("div", { class: "v" }, `${result.coverage.right_eligible} 家（排除 ${result.coverage.right_excluded}）`)),
        h("div", { class: "s" }, h("div", { class: "l" }, "调整后最大 |SMD|"), h("div", { class: "v" }, `${fmt.num(result.balance.left_max_smd_adjusted, 2)} → ${fmt.num(result.balance.right_max_smd_adjusted, 2)}`))
      )
    );
    root.append(panel);
  }

  function compactValue(value) {
    if (value === null || value === undefined) return "—";
    if (typeof value === "object") {
      const text = JSON.stringify(value);
      return text.length > 120 ? text.slice(0, 120) + "…" : text;
    }
    const text = String(value);
    return text.length > 120 ? text.slice(0, 120) + "…" : text;
  }

  // =================================================================== DATA
  views.renderData = async function () {
    const root = $("#data-body");
    if (!root) return;
    let state;
    try {
      state = await api("/api/state");
    } catch (err) {
      root.innerHTML = "";
      root.append(warning(esc(err.message), "warn"));
      return;
    }
    root.innerHTML = "";
    const cards = h("div", { class: "cards" });

    const snap = state.latest_snapshot;
    cards.append(
      h(
        "div",
        { class: "card" },
        h("h3", null, "当前快照"),
        snap
          ? h(
              "div",
              { class: "kv" },
              h("div", { class: "k" }, "摘要"),
              h("div", { class: "v mono" }, snap.snapshot_hash),
              h("div", { class: "k" }, "创建"),
              h("div", { class: "v" }, fmt.ts(snap.created_at)),
              h("div", { class: "k" }, "接入序号"),
              h("div", { class: "v" }, String(snap.max_ingest_seq)),
              ...Object.entries(snap.row_counts).flatMap(([t, n]) => [h("div", { class: "k" }, t), h("div", { class: "v" }, fmt.num(n) + " 行")])
            )
          : h("p", { style: "color:var(--muted)" }, "尚无快照。")
      )
    );
    cards.append(
      h(
        "div",
        { class: "card" },
        h("h3", null, "报告队列"),
        h(
          "div",
          { class: "kv" },
          ...Object.entries(state.reports_by_status).map(([status, n]) => [h("div", { class: "k" }, fmt.status(status)), h("div", { class: "v" }, fmt.num(n))]),
          h("div", { class: "k" }, "引擎版本"),
          h("div", { class: "v mono" }, state.engine_version)
        ),
        h(
          "p",
          { style: "font-size:11.5px;color:var(--muted);margin-top:8px" },
          "同一组合只允许一个有效结果：新版本入队会作废旧任务，旧任务即使跑完也不能写入。"
        )
      )
    );
    cards.append(
      h(
        "div",
        { class: "card" },
        h("h3", null, "退款回补（迟到数据）"),
        h(
          "p",
          { style: "font-size:12px;color:var(--muted)" },
          "回补文件包含退款截止日之后到账的退款。回补只写入落地表并生成新快照；已有报告保持原样，可选择基于新快照重算。"
        ),
        h(
          "div",
          { class: "actions", style: "margin-top:8px" },
          h(
            "button",
            {
              class: "btn primary small",
              onclick: async (e) => {
                const btn = e.currentTarget;
                btn.disabled = true;
                btn.innerHTML = '<span class="spinner"></span> 回补中…';
                try {
                  const result = await api("/api/ingest/backfill", { method: "POST", body: JSON.stringify({}) });
                  PL.toast(`回补完成：更新 ${result.batch.updated} 行、新增 ${result.batch.inserted} 行，新快照 ${fmt.shortHash(result.snapshot.snapshot_hash)}`);
                  await PL.loadBootstrap();
                  views.renderData();
                } catch (err) {
                  PL.toast(err.message, true);
                  btn.disabled = false;
                  btn.textContent = "回补退款并生成新快照";
                }
              },
            },
            "回补退款并生成新快照"
          ),
          h(
            "button",
            {
              class: "btn small",
              onclick: async (e) => {
                const btn = e.currentTarget;
                btn.disabled = true;
                btn.innerHTML = '<span class="spinner"></span> 接入中…';
                try {
                  const result = await api("/api/ingest/file", {
                    method: "POST",
                    body: JSON.stringify({ table: "exposure", filename: "exposure.csv" }),
                  });
                  PL.toast(`曝光重新接入完成：新增 ${result.batch.inserted}、更新 ${result.batch.updated}、未变 ${result.batch.unchanged}`);
                  await PL.loadBootstrap();
                  views.renderData();
                } catch (err) {
                  PL.toast(err.message, true);
                  btn.disabled = false;
                  btn.textContent = "重新接入曝光文件";
                }
              },
            },
            "重新接入曝光文件"
          )
        )
      )
    );
    root.append(h("div", { class: "subhead", style: "margin-top:0" }, h("h3", null, "数据流水")), cards);

    // batches
    const batchPanel = h(
      "div",
      { class: "panel", style: "margin-top:14px" },
      h("div", { class: "panel-head" }, h("h2", null, "接入批次"), h("span", { class: "hint" }, "重复文件不会重复计入")),
      h("div", { class: "panel-body" })
    );
    if (!state.batches.length) {
      batchPanel.lastChild.append(h("p", { style: "color:var(--muted)" }, "还没有接入批次。"));
    }
    for (const batch of state.batches) {
      batchPanel.lastChild.append(
        h(
          "div",
          { class: "batch-row" },
          h(
            "div",
            null,
            h("div", { class: "f" }, `${batch.table_name} · ${batch.source_file}`),
            h(
              "div",
              { class: "m" },
              `${fmt.ts(batch.finished_at)} · 读取 ${fmt.num(batch.rows_read)} · 新增 ${fmt.num(batch.inserted)} · 更新 ${fmt.num(batch.updated)} · 未变 ${fmt.num(batch.unchanged)} · 拒绝 ${fmt.num(batch.rejected)}`
            ),
            h("div", { class: "mono", style: "color:var(--muted)" }, `sha256 ${fmt.shortHash(batch.source_sha256, 16)}`)
          ),
          h("div", null, h("span", { class: "tag " + (batch.status === "succeeded" ? "promo" : "bad") }, batch.status === "succeeded" ? "成功" : "失败"))
        )
      );
    }
    root.append(batchPanel);

    if (state.rejects.length) {
      const rejectPanel = h(
        "div",
        { class: "panel", style: "margin-top:14px" },
        h("div", { class: "panel-head" }, h("h2", null, "被拒绝的原始行"), h("span", { class: "hint" }, "逐行隔离，不进入分析")),
        h("div", { class: "panel-body" })
      );
      for (const reject of state.rejects) {
        rejectPanel.lastChild.append(
          h(
            "div",
            { class: "reject-row" },
            h("span", { class: "mono" }, `${reject.table_name} 第 ${reject.source_line} 行`),
            " ",
            h("span", { class: "why" }, reject.reason)
          )
        );
      }
      root.append(rejectPanel);
    }

    // versions
    const versionPanel = h(
      "div",
      { class: "panel", style: "margin-top:14px" },
      h("div", { class: "panel-head" }, h("h2", null, "规则与观察窗版本"), h("span", { class: "hint" }, "旧版本不可修改，只可被新版本取代")),
      h("div", { class: "panel-body" })
    );
    const vgrid = h("div", { class: "compare-grid" });
    const ruleCard = h("div", { class: "card" }, h("h3", null, "归因规则"));
    for (const rule of state.versions.rules) {
      ruleCard.append(
        h(
          "div",
          { class: "reject-row" },
          h("span", { class: "mono" }, rule.rule_version),
          " ",
          h("span", { class: "tag " + (rule.status === "active" ? "promo" : "muted") }, rule.status === "active" ? "启用" : "已取代"),
          h("div", { class: "raw" }, `回看 ${rule.lookback_hours}h · ${rule.order_statuses.join("/")} · ${rule.channel_scope.join("/")} · ${rule.exposure_kinds.join("/")} · 同店${rule.require_same_store ? "是" : "否"}`)
        )
      );
    }
    vgrid.append(ruleCard);
    const cohortCard = h("div", { class: "card" }, h("h3", null, "观察窗"));
    for (const cohort of state.versions.cohorts) {
      cohortCard.append(
        h(
          "div",
          { class: "reject-row" },
          h("span", { class: "mono" }, cohort.cohort_version),
          " ",
          h("span", { class: "tag " + (cohort.status === "active" ? "promo" : "muted") }, cohort.status === "active" ? "启用" : "已取代"),
          h("div", { class: "raw" }, `活动 ${cohort.campaign_start}~${cohort.campaign_end} · 期前 ${cohort.pre_start}~${cohort.pre_end} · 退款截止 ${cohort.refund_cutoff}`)
        )
      );
    }
    vgrid.append(cohortCard);
    versionPanel.lastChild.append(vgrid);
    versionPanel.lastChild.append(
      h("div", { class: "subhead" }, h("h3", null, "方法版本")),
      h(
        "div",
        { class: "kv" },
        ...state.versions.methods.flatMap((m) => [h("div", { class: "k" }, m.method_version), h("div", { class: "v", style: "text-align:left" }, m.label + " — " + m.description)])
      )
    );
    root.append(versionPanel);
  };
})();
