const elements = {
  picker: document.querySelector("#report-date"),
  status: document.querySelector("#report-status"),
  content: document.querySelector("#report-content"),
  comparisonBody: document.querySelector("#comparison-body"),
  comparisonWrap: document.querySelector("#comparison-table-wrap"),
  comparisonEmpty: document.querySelector("#comparison-empty"),
  findings: document.querySelector("#report-findings"),
  focusSymbol: document.querySelector("#focus-symbol"),
  focusSummary: document.querySelector("#focus-summary"),
  focusBody: document.querySelector("#focus-body"),
  focusHorizonChart: document.querySelector("#focus-horizon-chart"),
  focusTimelineChart: document.querySelector("#focus-timeline-chart"),
  caseStudyBody: document.querySelector("#case-study-body"),
  caseStudyWrap: document.querySelector("#case-study-table-wrap"),
  caseStudyEmpty: document.querySelector("#case-study-empty"),
  caseDetailList: document.querySelector("#case-detail-list"),
  overallBody: document.querySelector("#overall-body"),
  eventTypeBody: document.querySelector("#event-type-body"),
};

const HORIZONS = [0, 1, 2, 5, 20];
let activeReport = null;

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

function median(values) {
  if (!values.length) return null;
  const sorted = [...values].sort((left, right) => left - right);
  const middle = Math.floor(sorted.length / 2);
  return sorted.length % 2
    ? sorted[middle]
    : (sorted[middle - 1] + sorted[middle]) / 2;
}

function focusStatistics(studies, horizon) {
  const raw = studies.filter((study) => study[`raw_return_${horizon}d`] != null);
  const dateValues = new Map(
    raw.map((study) => [study.reaction_date, Number(study[`raw_return_${horizon}d`])]),
  );
  const weight = (study) => Number(study.event_weight ?? 1);
  const effective = raw.reduce((total, study) => total + weight(study), 0);
  const peer = raw.filter(
    (study) => study[`exploratory_relative_return_${horizon}d`] != null,
  );
  const peerWeight = peer.reduce((total, study) => total + weight(study), 0);
  return {
    events: raw.length,
    dateEvents: dateValues.size,
    effective,
    mean: effective
      ? raw.reduce(
          (total, study) =>
            total + weight(study) * Number(study[`raw_return_${horizon}d`]),
          0,
        ) / effective
      : null,
    median: median([...dateValues.values()]),
    winRate: effective
      ? raw.reduce(
          (total, study) =>
            total + weight(study) * (Number(study[`raw_return_${horizon}d`]) > 0 ? 1 : 0),
          0,
        ) / effective
      : null,
    peerMean: peerWeight
      ? peer.reduce(
          (total, study) =>
            total +
            weight(study) * Number(study[`exploratory_relative_return_${horizon}d`]),
          0,
        ) / peerWeight
      : null,
  };
}

const SVG_NS = "http://www.w3.org/2000/svg";

function svgNode(name, attributes = {}, content = null) {
  const node = document.createElementNS(SVG_NS, name);
  Object.entries(attributes).forEach(([key, value]) => node.setAttribute(key, value));
  if (content !== null) node.textContent = content;
  return node;
}

function chartGeometry(values) {
  const left = 58;
  const right = 18;
  const top = 42;
  const bottom = 45;
  const width = 720 - left - right;
  const height = 300 - top - bottom;
  let minimum = Math.min(0, ...values);
  let maximum = Math.max(0, ...values);
  const span = maximum - minimum || 0.02;
  minimum -= span * 0.12;
  maximum += span * 0.12;
  return {
    left,
    top,
    width,
    height,
    minimum,
    maximum,
    y: (value) => top + ((maximum - value) / (maximum - minimum)) * height,
  };
}

function drawChartGrid(svg, geometry) {
  for (let index = 0; index <= 4; index += 1) {
    const value = geometry.maximum -
      ((geometry.maximum - geometry.minimum) * index) / 4;
    const y = geometry.y(value);
    svg.append(
      svgNode("line", {
        x1: geometry.left,
        y1: y,
        x2: geometry.left + geometry.width,
        y2: y,
        class: Math.abs(value) < 0.00001 ? "report-chart-zero" : "report-chart-grid",
      }),
      svgNode(
        "text",
        { x: geometry.left - 8, y: y + 4, "text-anchor": "end", class: "report-chart-label" },
        `${(value * 100).toFixed(1)}%`,
      ),
    );
  }
  const zeroY = geometry.y(0);
  svg.append(svgNode("line", {
    x1: geometry.left,
    y1: zeroY,
    x2: geometry.left + geometry.width,
    y2: zeroY,
    class: "report-chart-zero",
  }));
}

function renderHorizonChart(studies) {
  const svg = elements.focusHorizonChart;
  svg.replaceChildren();
  const points = HORIZONS.map((horizon) => ({
    horizon,
    ...focusStatistics(studies, horizon),
  }));
  const series = [
    { label: "加重平均", key: "mean", className: "report-chart-mean", color: "#38bdf8" },
    { label: "反応日中央値", key: "median", className: "report-chart-median", color: "#f8c36a" },
    { label: "peer差", key: "peerMean", className: "report-chart-peer", color: "#a78bfa" },
  ];
  const values = series.flatMap((item) =>
    points.map((point) => point[item.key]).filter((value) => value != null),
  );
  if (!values.length) return;
  const geometry = chartGeometry(values);
  drawChartGrid(svg, geometry);
  const x = (horizon) => geometry.left + (horizon / 20) * geometry.width;

  series.forEach((item, index) => {
    const available = points.filter((point) => point[item.key] != null);
    if (!available.length) return;
    const d = available
      .map((point, pointIndex) =>
        `${pointIndex ? "L" : "M"}${x(point.horizon)},${geometry.y(point[item.key])}`,
      )
      .join(" ");
    svg.append(svgNode("path", { d, class: item.className }));
    available.forEach((point) => {
      const circle = svgNode("circle", {
        cx: x(point.horizon),
        cy: geometry.y(point[item.key]),
        r: 4,
        fill: item.color,
        class: "report-chart-point",
      });
      circle.append(svgNode("title", {}, `${item.label} ${percent(point[item.key], true)}`));
      svg.append(circle);
    });
    const legendX = geometry.left + index * 142;
    svg.append(
      svgNode("line", {
        x1: legendX,
        y1: 18,
        x2: legendX + 22,
        y2: 18,
        class: item.className,
      }),
      svgNode("text", { x: legendX + 28, y: 22, class: "report-chart-label" }, item.label),
    );
  });

  HORIZONS.forEach((horizon) => {
    svg.append(svgNode(
      "text",
      {
        x: x(horizon),
        y: geometry.top + geometry.height + 25,
        "text-anchor": "middle",
        class: "report-chart-label",
      },
      horizon === 0 ? "反応日" : `+${horizon}日`,
    ));
  });
}

function renderTimelineChart(studies) {
  const svg = elements.focusTimelineChart;
  svg.replaceChildren();
  const byDate = new Map();
  studies.forEach((study) => {
    if (study.raw_return_0d == null || byDate.has(study.reaction_date)) return;
    byDate.set(study.reaction_date, {
      date: study.reaction_date,
      value: Number(study.raw_return_0d),
      volume: Number(study.reaction_volume_ratio_60d || 0),
      articles: studies.filter((item) => item.reaction_date === study.reaction_date).length,
    });
  });
  const points = [...byDate.values()].sort((left, right) =>
    left.date.localeCompare(right.date),
  );
  if (!points.length) return;
  const geometry = chartGeometry(points.map((point) => point.value));
  drawChartGrid(svg, geometry);
  const times = points.map((point) => Date.parse(`${point.date}T00:00:00Z`));
  const minTime = Math.min(...times);
  const maxTime = Math.max(...times);
  const x = (time) => maxTime === minTime
    ? geometry.left + geometry.width / 2
    : geometry.left + ((time - minTime) / (maxTime - minTime)) * geometry.width;
  const zeroY = geometry.y(0);
  const barWidth = Math.min(24, geometry.width / Math.max(points.length * 1.8, 1));
  const labelEvery = Math.max(1, Math.ceil(points.length / 6));

  points.forEach((point, index) => {
    const pointX = x(times[index]);
    const pointY = geometry.y(point.value);
    const bar = svgNode("rect", {
      x: pointX - barWidth / 2,
      y: Math.min(zeroY, pointY),
      width: barWidth,
      height: Math.max(Math.abs(zeroY - pointY), 1),
      rx: 2,
      class: point.value >= 0 ? "report-chart-positive" : "report-chart-negative",
      opacity: 0.82,
    });
    const description = `${point.date} ${percent(point.value, true)} / ` +
      `${point.articles}記事 / 出来高比${number(point.volume, 2)}倍`;
    bar.append(svgNode("title", {}, description));
    const volumePoint = svgNode("circle", {
      cx: pointX,
      cy: pointY,
      r: Math.max(2.5, Math.min(6, point.volume * 3)),
      fill: point.value >= 0 ? "#86efac" : "#fca5a5",
      class: "report-chart-point",
    });
    volumePoint.append(svgNode("title", {}, description));
    svg.append(bar, volumePoint);
    if (index % labelEvery === 0 || index === points.length - 1) {
      svg.append(svgNode(
        "text",
        {
          x: pointX,
          y: geometry.top + geometry.height + 25,
          "text-anchor": "middle",
          class: "report-chart-label",
        },
        point.date.slice(5),
      ));
    }
  });
}

function renderFocusSymbol(report, symbol) {
  const focus = (report.symbol_focus || []).find((item) => item.symbol === symbol) || {};
  const studies = (report.case_studies || []).filter(
    (study) => study.symbol === symbol,
  );
  const reactionDates = [...new Set(studies.map((study) => study.reaction_date))].sort();
  const eventTypes = new Map();
  const categories = new Map();
  studies.forEach((study) => {
    const type = study.event_type || "unknown";
    eventTypes.set(type, (eventTypes.get(type) || 0) + 1);
    const category = study.category || "unknown";
    categories.set(category, (categories.get(category) || 0) + 1);
  });
  const effective = studies.reduce(
    (total, study) => total + Number(study.event_weight ?? 1),
    0,
  );
  text("focus-events", number(focus.notion_article_events ?? studies.length, 0));
  text("focus-matched", number(studies.length, 0));
  text("focus-effective", number(effective, 1));
  text("focus-dates", number(reactionDates.length, 0));
  text(
    "focus-origins",
    Object.entries(focus.ticker_origins_inventory || {})
      .map(([origin, count]) => `${origin} ${count}`)
      .join(" / "),
  );
  text(
    "focus-categories",
    [...categories.entries()].map(([category, count]) => `${category} ${count}`).join(" / "),
  );
  text(
    "focus-types",
    [...eventTypes.entries()].map(([type, count]) => `${type} ${count}`).join(" / "),
  );

  const day0 = focusStatistics(studies, 0);
  const day2 = focusStatistics(studies, 2);
  const dateRange = reactionDates.length
    ? `${reactionDates[0]}〜${reactionDates.at(-1)}`
    : "反応日なし";
  elements.focusSummary.textContent =
    `${symbol}はNotion ${focus.notion_article_events ?? studies.length}件のうち` +
    `${studies.length}件を日足へ接続（${dateRange}）。` +
    `反応日の加重平均は${percent(day0.mean, true)}` +
    `、2日後までの加重平均は${percent(day2.mean, true)}` +
    `（${day2.events}件観測）です。記事数が少ないため、因果効果ではなく` +
    "同一銘柄のケース集積として読みます。";

  elements.focusBody.replaceChildren();
  HORIZONS.forEach((horizon) => {
    const stats = focusStatistics(studies, horizon);
    row(elements.focusBody, [
      horizonLabel(horizon),
      number(stats.events, 0),
      number(stats.dateEvents, 0),
      number(stats.effective, 1),
      percent(stats.mean, true),
      percent(stats.median, true),
      percent(stats.winRate),
      percent(stats.peerMean, true),
    ]);
  });
  renderHorizonChart(studies);
  renderTimelineChart(studies);
}

function renderTickerFocus(report) {
  const symbols = report.symbol_focus || [];
  elements.focusSymbol.replaceChildren();
  symbols.forEach((focus) => {
    const option = document.createElement("option");
    option.value = focus.symbol;
    const notionEvents = focus.notion_article_events ?? focus.article_events ?? 0;
    const matchedEvents = focus.matched_events ?? focus.article_events ?? 0;
    option.textContent =
      `${focus.symbol}（Notion ${notionEvents} / 接続 ${matchedEvents}）`;
    elements.focusSymbol.append(option);
  });
  const requested = new URL(window.location.href).searchParams.get("symbol");
  const selected = symbols.some((focus) => focus.symbol === requested)
    ? requested
    : report.focus_symbol || symbols[0]?.symbol;
  if (!selected) return;
  elements.focusSymbol.value = selected;
  renderFocusSymbol(report, selected);
}

function horizonLabel(horizon) {
  return horizon === 0
    ? "反応日（1取引日累計）"
    : `+${horizon}日（${horizon + 1}取引日累計）`;
}

function detailTable(title, headers, rows) {
  const section = document.createElement("section");
  section.className = "case-metric-section";
  const heading = document.createElement("h4");
  heading.textContent = title;
  const wrap = document.createElement("div");
  wrap.className = "report-table-wrap";
  const table = document.createElement("table");
  const thead = document.createElement("thead");
  const headRow = document.createElement("tr");
  headers.forEach((label) => {
    const th = document.createElement("th");
    th.textContent = label;
    headRow.append(th);
  });
  thead.append(headRow);
  const tbody = document.createElement("tbody");
  rows.forEach((values) => row(tbody, values));
  table.append(thead, tbody);
  wrap.append(table);
  section.append(heading, wrap);
  return section;
}

function metricGrid(study, context) {
  const grid = document.createElement("dl");
  grid.className = "case-metric-grid";
  const items = [
    ["イベント日", study.event_date],
    ["反応候補日", study.candidate_date],
    ["反応取引日", study.reaction_date],
    [
      "ticker根拠",
      `${study.ticker_origin || "unknown"} / ${study.ticker_evidence || "—"}`,
    ],
    ["カテゴリ", study.category || "unknown"],
    ["出典", study.source || "—"],
    ["時刻品質", `${study.timing_quality || "—"} / ${study.timing_bucket || "—"}`],
    ["センチメント", study.sentiment || "—"],
    ["分類信頼度", number(study.confidence, 2)],
    ["重要度", number(study.importance, 0)],
    ["同日同種記事数", number(study.event_group_size, 0)],
    ["イベント重み", number(study.event_weight, 3)],
    ["20日窓の重複数", number(study.overlap_count, 0)],
    ["直前5取引日", percent(study.pre_event_return_5d, true)],
    ["直前20取引日", percent(study.pre_event_return_20d, true)],
    ["反応日出来高 / 過去60日中央値", study.reaction_volume_ratio_60d == null
      ? "—"
      : `${number(study.reaction_volume_ratio_60d, 2)}倍`],
    ["同規模の過去変動", context
      ? `${number(context.similar_move_count, 0)}件（${context.move_direction === "down" ? "下落" : "上昇"}）`
      : "—"],
  ];
  items.forEach(([label, value]) => {
    const item = document.createElement("div");
    const dt = document.createElement("dt");
    dt.textContent = label;
    const dd = document.createElement("dd");
    dd.textContent = value ?? "—";
    item.append(dt, dd);
    grid.append(item);
  });
  return grid;
}

function renderCaseDetails(report, studies) {
  elements.caseDetailList.replaceChildren();
  studies.forEach((study) => {
    const article = document.createElement("details");
    article.className = "case-detail";
    const toggle = document.createElement("summary");
    toggle.textContent =
      `${study.symbol} · ${study.reaction_date} · ${study.headline || "見出しなし"}`;
    const header = document.createElement("header");
    const title = document.createElement("h3");
    title.textContent = `${study.symbol} · ${study.reaction_date}`;
    const headline = document.createElement("p");
    headline.textContent = `${study.event_type || "unknown"} · ${study.headline || "見出しなし"}`;
    header.append(title, headline);
    if (study.summary_ja) {
      const summary = document.createElement("p");
      summary.className = "case-source-summary";
      summary.textContent = study.summary_ja;
      header.append(summary);
    }
    if (study.my_take) {
      const take = document.createElement("p");
      take.className = "case-source-take";
      take.textContent = `収集時の見立て: ${study.my_take}`;
      header.append(take);
    }

    const context = study.historical_move_context;
    const horizonRows = HORIZONS.map((horizon) => {
      const peerCount = Number(study[`peer_count_${horizon}d`] || 0);
      const abnormal = study[`abnormal_return_${horizon}d`];
      return [
        horizonLabel(horizon),
        percent(study[`raw_return_${horizon}d`], true),
        rarityLabel(study, horizon),
        percent(study[`historical_percentile_${horizon}d`]),
        number(study[`historical_observations_${horizon}d`], 0),
        number(peerCount, 0),
        percent(study[`exploratory_benchmark_return_${horizon}d`], true),
        percent(study[`exploratory_relative_return_${horizon}d`], true),
        abnormal == null && peerCount > 0
          ? `—（${report.min_peers}社未満）`
          : percent(abnormal, true),
      ];
    });
    const sections = [
      detailTable(
        "期間別の観測値・過去分布・同業比較",
        ["期間", "観測リターン", "過去分布での位置", "累積分位", "過去観測数", "peer数", "peer平均", "peerとの差", "正式abnormal"],
        horizonRows,
      ),
    ];
    if (context) {
      sections.push(
        detailTable(
          `同程度以上の過去変動後（${number(context.similar_move_count, 0)}件）`,
          ["先の期間", "平均", "中央値", "上昇率"],
          [1, 5, 20].map((days) => [
            `${days}取引日後`,
            percent(context[`forward_mean_${days}d`], true),
            percent(context[`forward_median_${days}d`], true),
            percent(context[`forward_win_rate_${days}d`]),
          ]),
        ),
      );
    }
    article.append(toggle, header, metricGrid(study, context), ...sections);
    elements.caseDetailList.append(article);
  });
}

function renderCaseStudies(report, symbol = null) {
  const studies = (report.case_studies || []).filter(
    (study) => !symbol || study.symbol === symbol,
  );
  elements.caseStudyBody.replaceChildren();
  elements.caseDetailList.replaceChildren();
  if (!studies.length) {
    elements.caseStudyWrap.hidden = true;
    elements.caseStudyEmpty.hidden = false;
    elements.caseStudyEmpty.textContent =
      `${symbol || "選択中の銘柄"}で日足へ接続できたイベントはまだありません。`;
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
  renderCaseDetails(report, studies);
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
  activeReport = report;
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
  renderTickerFocus(report);
  renderCaseStudies(report, elements.focusSymbol.value);
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

elements.focusSymbol.addEventListener("change", () => {
  if (!activeReport) return;
  renderFocusSymbol(activeReport, elements.focusSymbol.value);
  renderCaseStudies(activeReport, elements.focusSymbol.value);
  const url = new URL(window.location.href);
  url.searchParams.set("symbol", elements.focusSymbol.value);
  history.replaceState(null, "", url);
});

start();
