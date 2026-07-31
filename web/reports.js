const elements = {
  picker: document.querySelector("#report-date"),
  status: document.querySelector("#report-status"),
  content: document.querySelector("#report-content"),
  comparisonBody: document.querySelector("#comparison-body"),
  comparisonWrap: document.querySelector("#comparison-table-wrap"),
  comparisonEmpty: document.querySelector("#comparison-empty"),
  findings: document.querySelector("#report-findings"),
  caseStudyBody: document.querySelector("#case-study-body"),
  caseStudyWrap: document.querySelector("#case-study-table-wrap"),
  caseStudyEmpty: document.querySelector("#case-study-empty"),
  overallBody: document.querySelector("#overall-body"),
  eventTypeBody: document.querySelector("#event-type-body"),
};

const countIds = {
  news_pages: "count-pages",
  ticker_events: "count-events",
  matched_events: "count-matched",
  unmatched_events: "count-unmatched",
  date_only_events: "count-date-only",
  overlapping_events: "count-overlap",
};

function text(id, value) {
  document.querySelector(`#${id}`).textContent = value ?? "—";
}

function number(value, digits = 1) {
  if (value === null || value === undefined) return "—";
  return Number(value).toLocaleString("ja-JP", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
}

function percent(value, signed = false) {
  if (value === null || value === undefined) return "—";
  const numeric = Number(value) * 100;
  const sign = signed && numeric > 0 ? "+" : "";
  return `${sign}${numeric.toFixed(2)}%`;
}

function metricLabel(metric) {
  return metric === "abnormal_return" ? "市場調整後" : "調整済み株価";
}

function row(target, values, classNames = []) {
  const tr = document.createElement("tr");
  values.forEach((value, index) => {
    const td = document.createElement("td");
    td.textContent = value;
    if (classNames[index]) td.className = classNames[index];
    tr.append(td);
  });
  target.append(tr);
}

function overallRows(summary) {
  return summary.filter(
    (item) =>
      item.sample === "all" &&
      item.dimension === "all" &&
      item.group_value === "all",
  );
}

function renderObservation(report) {
  elements.findings.replaceChildren();
  if (report.findings?.length) {
    report.findings.forEach((finding) => {
      const article = document.createElement("article");
      article.className = `report-finding ${finding.level || "observation"}`;
      const heading = document.createElement("strong");
      heading.textContent = finding.title;
      const body = document.createElement("p");
      body.textContent = finding.body;
      article.append(heading, body);
      elements.findings.append(article);
    });
    return;
  }
  const abnormal5d = overallRows(report.summary).find(
    (item) => item.metric === "abnormal_return" && item.horizon === 5,
  );
  const matched = report.counts.matched_events;
  if (!abnormal5d || abnormal5d.weighted_mean_return === null) {
    const fallback = document.createElement("p");
    fallback.textContent =
      `日足へ接続できたイベントは${matched}件です。比較可能なpeerまたは観測窓が` +
      "不足しているため、市場調整後5日リターンはまだ算出できません。";
    elements.findings.append(fallback);
    return;
  }
  const direction = abnormal5d.weighted_mean_return >= 0 ? "上回りました" : "下回りました";
  const fallback = document.createElement("p");
  fallback.textContent =
    `イベント後5取引日の平均は、同subsector benchmarkを` +
    `${Math.abs(Number(abnormal5d.weighted_mean_return) * 100).toFixed(2)}ポイント${direction}。` +
    `ただし実効件数は${number(abnormal5d.effective_events)}件です。`;
  elements.findings.append(fallback);
}

function latestObservedHorizon(study) {
  return [20, 5, 2, 1, 0].find(
    (horizon) => study[`raw_return_${horizon}d`] !== null,
  );
}

function rarityLabel(study, horizon) {
  const value = study[`raw_return_${horizon}d`];
  const percentile = study[`historical_percentile_${horizon}d`];
  const observations = study[`historical_observations_${horizon}d`] || 0;
  if (value === null || percentile === null) return "—";
  if (observations < 252) return `不足（${number(observations, 0)}観測）`;
  const tail = value < 0 ? Number(percentile) : 1 - Number(percentile);
  return `${value < 0 ? "下位" : "上位"}${(tail * 100).toFixed(1)}%`;
}

function eventCell(tr, study) {
  const td = document.createElement("td");
  td.className = "report-event-cell";
  const symbol = document.createElement("strong");
  symbol.textContent = `${study.symbol} · ${study.event_type || "unknown"}`;
  const headline = document.createElement("span");
  headline.textContent = study.headline || "見出しなし";
  td.append(symbol, headline);
  tr.append(td);
}

function renderCaseStudies(report) {
  const studies = report.case_studies || [];
  elements.caseStudyBody.replaceChildren();
  if (!studies.length) {
    elements.caseStudyWrap.hidden = true;
    elements.caseStudyEmpty.hidden = false;
    elements.caseStudyEmpty.textContent =
      "日足とニュースの両方へ接続できたイベントはまだありません。";
    return;
  }
  elements.caseStudyWrap.hidden = false;
  elements.caseStudyEmpty.hidden = true;
  studies.forEach((study) => {
    const horizon = latestObservedHorizon(study);
    if (horizon === undefined) return;
    const peers = study[`peer_count_${horizon}d`] || 0;
    const relative = study[`exploratory_relative_return_${horizon}d`];
    const context = study.historical_move_context;
    const tr = document.createElement("tr");
    const dateCell = document.createElement("td");
    dateCell.textContent = study.reaction_date;
    tr.append(dateCell);
    eventCell(tr, study);
    [
      percent(study.pre_event_return_20d, true),
      `${horizon}日 ${percent(study[`raw_return_${horizon}d`], true)}`,
      rarityLabel(study, horizon),
      relative == null ? "—" : `${percent(relative, true)} / ${peers}社`,
      study.reaction_volume_ratio_60d == null
        ? "—"
        : `${Number(study.reaction_volume_ratio_60d).toFixed(2)}倍`,
      !context || context.similar_move_count < 20 || context.forward_mean_5d == null
        ? "—"
        : `${percent(context.forward_mean_5d, true)} / 勝率${percent(context.forward_win_rate_5d)}`,
    ].forEach((value) => {
      const td = document.createElement("td");
      td.textContent = value;
      tr.append(td);
    });
    elements.caseStudyBody.append(tr);
  });
}

function renderComparison(report) {
  const previous = report.previous_report_date;
  const rows = (report.comparison?.overall || []).filter(
    (item) => item.metric === "abnormal_return",
  );
  elements.comparisonBody.replaceChildren();
  text(
    "comparison-label",
    previous ? `${previous}版との比較` : "比較元となる過去版はまだありません",
  );
  if (!previous || !rows.length) {
    elements.comparisonWrap.hidden = true;
    elements.comparisonEmpty.hidden = false;
    elements.comparisonEmpty.textContent =
      "この日が履歴の起点です。次回以降は、市場調整後リターンの変化を期間別に表示します。";
    return;
  }
  elements.comparisonWrap.hidden = false;
  elements.comparisonEmpty.hidden = true;
  rows.forEach((item) =>
    row(elements.comparisonBody, [
      metricLabel(item.metric),
      `${item.horizon}日`,
      percent(item.current),
      percent(item.previous),
      percent(item.delta, true),
    ]),
  );
}

function renderTables(report) {
  elements.overallBody.replaceChildren();
  overallRows(report.summary).forEach((item) =>
    row(elements.overallBody, [
      metricLabel(item.metric),
      `${item.horizon}日`,
      number(item.events, 0),
      number(item.effective_events),
      percent(item.weighted_mean_return),
      percent(item.median_return),
      percent(item.weighted_win_rate),
      `${percent(item.ci95_low)} – ${percent(item.ci95_high)}`,
    ]),
  );

  elements.eventTypeBody.replaceChildren();
  report.summary
    .filter(
      (item) =>
        item.sample === "non_overlapping" &&
        item.dimension === "event_type" &&
        item.metric === "abnormal_return" &&
        [0, 5, 20].includes(item.horizon),
    )
    .forEach((item) =>
      row(elements.eventTypeBody, [
        item.group_value,
        `${item.horizon}日`,
        number(item.events, 0),
        percent(item.weighted_mean_return),
        percent(item.median_return),
        percent(item.weighted_win_rate),
      ]),
    );
}

function renderReport(report) {
  text("report-day", report.report_date);
  text("daily-through", report.daily_through);
  text("notion-through", report.notion_through);
  Object.entries(countIds).forEach(([key, id]) => text(id, number(report.counts[key], 0)));
  text(
    "unmatched-symbols",
    `未接続銘柄: ${(report.unmatched_symbols || []).join(", ") || "なし"} / ` +
      `benchmark最低peer数: ${report.min_peers}`,
  );
  renderObservation(report);
  renderCaseStudies(report);
  renderComparison(report);
  renderTables(report);
  elements.status.hidden = true;
  elements.content.hidden = false;
}

async function loadReport(reportDate) {
  elements.status.hidden = false;
  elements.status.textContent = `${reportDate}版を読み込んでいます…`;
  elements.content.hidden = true;
  const response = await fetch(`/api/analysis/reports/${encodeURIComponent(reportDate)}`, {
    headers: { Accept: "application/json" },
  });
  if (!response.ok) throw new Error(`report HTTP ${response.status}`);
  const report = await response.json();
  renderReport(report);
  const url = new URL(window.location.href);
  url.searchParams.set("date", reportDate);
  history.replaceState(null, "", url);
}

async function start() {
  try {
    const response = await fetch("/api/analysis/reports", {
      headers: { Accept: "application/json" },
    });
    if (!response.ok) throw new Error(`index HTTP ${response.status}`);
    const index = await response.json();
    if (!index.reports.length) {
      elements.status.textContent =
        "分析レポートはまだありません。次回の日次イベントスタディ完了後に表示されます。";
      return;
    }
    elements.picker.replaceChildren();
    index.reports.forEach((report) => {
      const option = document.createElement("option");
      option.value = report.report_date;
      option.textContent = `${report.report_date}（接続 ${report.counts.matched_events}件）`;
      elements.picker.append(option);
    });
    elements.picker.disabled = false;
    const requested = new URL(window.location.href).searchParams.get("date");
    const selected = index.reports.some((item) => item.report_date === requested)
      ? requested
      : index.latest_report_date;
    elements.picker.value = selected;
    await loadReport(selected);
  } catch (error) {
    console.error(error);
    elements.status.textContent =
      "レポートを読み込めませんでした。時間をおいて再読み込みしてください。";
  }
}

elements.picker.addEventListener("change", () => {
  loadReport(elements.picker.value).catch((error) => {
    console.error(error);
    elements.status.hidden = false;
    elements.status.textContent = "選択したレポートを読み込めませんでした。";
  });
});

start();
