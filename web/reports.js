const elements = {
  picker: document.querySelector("#report-date"),
  status: document.querySelector("#report-status"),
  content: document.querySelector("#report-content"),
  comparisonBody: document.querySelector("#comparison-body"),
  comparisonWrap: document.querySelector("#comparison-table-wrap"),
  comparisonEmpty: document.querySelector("#comparison-empty"),
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
  const abnormal5d = overallRows(report.summary).find(
    (item) => item.metric === "abnormal_return" && item.horizon === 5,
  );
  const matched = report.counts.matched_events;
  if (!abnormal5d || abnormal5d.weighted_mean_return === null) {
    text(
      "report-observation",
      `日足へ接続できたイベントは${matched}件です。比較可能なpeerまたは観測窓が不足しているため、` +
        "市場調整後5日リターンはまだ算出できません。日足corpusの拡充に伴い自動更新されます。",
    );
    return;
  }
  const direction = abnormal5d.weighted_mean_return >= 0 ? "上回りました" : "下回りました";
  text(
    "report-observation",
    `イベント後5取引日の平均は、同subsector benchmarkを` +
      `${Math.abs(Number(abnormal5d.weighted_mean_return) * 100).toFixed(2)}ポイント${direction}。` +
      `ただし実効件数は${number(abnormal5d.effective_events)}件で、因果ではなく観測上の関連です。`,
  );
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
