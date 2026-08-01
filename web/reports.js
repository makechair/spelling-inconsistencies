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
  focusHorizonInterpretation: document.querySelector("#focus-horizon-interpretation"),
  focusTimelineInterpretation: document.querySelector("#focus-timeline-interpretation"),
  analogEvent: document.querySelector("#analog-event"),
  analogSummary: document.querySelector("#analog-summary"),
  analogChart: document.querySelector("#analog-chart"),
  analogDecision: document.querySelector("#analog-decision"),
  analogInterpretation: document.querySelector("#analog-interpretation"),
  returnSurfaceBucket: document.querySelector("#return-surface-bucket"),
  returnSurfaceSummary: document.querySelector("#return-surface-summary"),
  returnSurfaceChart: document.querySelector("#return-surface-chart"),
  returnPathChart: document.querySelector("#return-path-chart"),
  returnSurfaceDecision: document.querySelector("#return-surface-decision"),
  returnSurfaceInterpretation: document.querySelector("#return-surface-interpretation"),
  walkForwardSummary: document.querySelector("#walk-forward-summary"),
  walkForwardBody: document.querySelector("#walk-forward-body"),
  walkForwardExampleBody: document.querySelector("#walk-forward-example-body"),
  peerSummary: document.querySelector("#peer-summary"),
  peerChart: document.querySelector("#peer-chart"),
  peerInterpretation: document.querySelector("#peer-interpretation"),
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

function quantile(values, probability) {
  if (!values.length) return null;
  const sorted = [...values].sort((left, right) => left - right);
  const position = (sorted.length - 1) * probability;
  const lower = Math.floor(position);
  const upper = Math.ceil(position);
  if (lower === upper) return sorted[lower];
  return sorted[lower] + (sorted[upper] - sorted[lower]) * (position - lower);
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
  const reactionReturns = [...dateValues.values()];
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
    reactionReturns,
    median: median(reactionReturns),
    reactionWinRate: reactionReturns.length
      ? reactionReturns.filter((value) => value > 0).length / reactionReturns.length
      : null,
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

function renderPeerChart(study) {
  const svg = elements.peerChart;
  svg.replaceChildren();
  elements.peerSummary.textContent = "";
  elements.peerInterpretation.textContent = "同業比較に必要な日足がありません。";
  if (!study) return;

  const points = HORIZONS.map((horizon) => ({
    horizon,
    subject: study[`raw_return_${horizon}d`],
    peer: study[`exploratory_benchmark_return_${horizon}d`],
    relative: study[`exploratory_relative_return_${horizon}d`],
    peerCount: Number(study[`peer_count_${horizon}d`] || 0),
  })).filter((point) => point.subject != null && point.peer != null);
  if (!points.length) return;

  const peerCount = Math.max(...points.map((point) => point.peerCount));
  elements.peerSummary.textContent =
    `${study.symbol}の${study.reaction_date}を基準に、前取引日終値からの累計リターンを` +
    `同subsectorの他${peerCount}社平均と比較しています。`;
  const values = points.flatMap((point) => [Number(point.subject), Number(point.peer)]);
  const geometry = chartGeometry(values);
  drawChartGrid(svg, geometry);
  const zeroY = geometry.y(0);
  const slot = geometry.width / HORIZONS.length;
  const barWidth = Math.min(24, slot * 0.23);
  const x = (horizon) => {
    const index = HORIZONS.indexOf(horizon);
    return geometry.left + slot * (index + 0.5);
  };

  points.forEach((point) => {
    const pointX = x(point.horizon);
    [
      { value: Number(point.subject), offset: -barWidth * 0.65, className: "report-chart-subject", label: study.symbol },
      { value: Number(point.peer), offset: barWidth * 0.65, className: "report-chart-peer-bar", label: "同業平均" },
    ].forEach((barSpec) => {
      const valueY = geometry.y(barSpec.value);
      const bar = svgNode("rect", {
        x: pointX + barSpec.offset - barWidth / 2,
        y: Math.min(zeroY, valueY),
        width: barWidth,
        height: Math.max(Math.abs(zeroY - valueY), 1),
        rx: 3,
        class: barSpec.className,
      });
      bar.append(svgNode("title", {},
        `${horizonLabel(point.horizon)} ${barSpec.label} ${percent(barSpec.value, true)}`,
      ));
      svg.append(
        bar,
        svgNode("text", {
          x: pointX + barSpec.offset,
          y: valueY + (barSpec.value >= 0 ? -7 : 15),
          "text-anchor": "middle",
          class: "report-chart-bar-value",
        }, percent(barSpec.value, true)),
      );
    });
  });

  HORIZONS.forEach((horizon) => {
    const point = points.find((item) => item.horizon === horizon);
    svg.append(
      svgNode("text", {
        x: x(horizon),
        y: geometry.top + geometry.height + 23,
        "text-anchor": "middle",
        class: "report-chart-label",
      }, horizon === 0 ? "反応日" : `+${horizon}日`),
      svgNode("text", {
        x: x(horizon),
        y: geometry.top + geometry.height + 42,
        "text-anchor": "middle",
        class: "report-chart-relative",
      }, point?.relative == null ? "—" : `個別差 ${percent(point.relative, true)}`),
    );
  });
  svg.append(
    svgNode("rect", { x: geometry.left, y: 10, width: 13, height: 13, rx: 2, class: "report-chart-subject" }),
    svgNode("text", { x: geometry.left + 19, y: 21, class: "report-chart-label" }, study.symbol),
    svgNode("rect", { x: geometry.left + 95, y: 10, width: 13, height: 13, rx: 2, class: "report-chart-peer-bar" }),
    svgNode("text", { x: geometry.left + 114, y: 21, class: "report-chart-label" }, `同業${peerCount}社平均`),
  );

  const primary = points.find((point) => point.horizon === 5) || points.at(-1);
  const subject = Number(primary.subject);
  const peer = Number(primary.peer);
  const relative = primary.relative == null
    ? subject - peer
    : Number(primary.relative);
  let reading;
  if (Math.sign(subject) !== Math.sign(peer) && subject !== 0 && peer !== 0) {
    reading = "銘柄と同業が逆方向で、セクター共通要因だけでは説明しにくい動きです。";
  } else if (Math.abs(relative) <= Math.max(Math.abs(subject) * 0.35, 0.01)) {
    reading = "銘柄と同業が同方向かつ差が比較的小さく、セクター共通要因の寄与が大きい動きです。";
  } else {
    reading = `同業と方向は共通しますが、${study.symbol}が${percent(relative, true)}乖離し、個別要因の候補が残ります。`;
  }
  const formal = primary.peerCount >= 3
    ? "正式なsubsector差として扱える最低3社を満たします。"
    : `比較対象は${primary.peerCount}社のため探索的な比較です。`;
  elements.peerInterpretation.textContent =
    `今回：${horizonLabel(primary.horizon)}で${study.symbol}${percent(subject, true)}、` +
    `同業平均${percent(peer, true)}、差${percent(relative, true)}。${reading}${formal}`;
}

function renderAnalogChart(studies, requestedDate = null) {
  const byDate = new Map();
  studies.forEach((study) => {
    if (
      study.raw_return_0d != null &&
      study.historical_move_context &&
      !byDate.has(study.reaction_date)
    ) {
      byDate.set(study.reaction_date, study);
    }
  });
  const candidates = [...byDate.values()].sort((left, right) =>
    right.reaction_date.localeCompare(left.reaction_date),
  );
  elements.analogEvent.replaceChildren();
  elements.analogChart.replaceChildren();
  elements.analogDecision.hidden = true;
  elements.analogDecision.replaceChildren();
  elements.analogSummary.textContent = "";
  elements.analogInterpretation.textContent = "同規模変動の過去統計がありません。";
  renderPeerChart(null);
  elements.analogEvent.disabled = !candidates.length;
  if (!candidates.length) return;

  candidates.forEach((study) => {
    const context = study.historical_move_context;
    const option = document.createElement("option");
    option.value = study.reaction_date;
    option.textContent = `${study.reaction_date} ${percent(study.raw_return_0d, true)}` +
      `（過去${number(context.similar_move_count, 0)}件）`;
    elements.analogEvent.append(option);
  });
  const adequate = candidates.find(
    (study) => Number(study.historical_move_context.similar_move_count || 0) >= 20,
  );
  const selected = candidates.find((study) => study.reaction_date === requestedDate) ||
    adequate || candidates[0];
  elements.analogEvent.value = selected.reaction_date;
  renderPeerChart(selected);

  const context = selected.historical_move_context;
  const count = Number(context.similar_move_count || 0);
  const move = Number(selected.raw_return_0d);
  const moveLabel = context.move_direction === "down" ? "下落" : "上昇";
  elements.analogSummary.textContent =
    `${selected.reaction_date}の${percent(move, true)}を基準に、過去の同程度以上の` +
    `${moveLabel}日${number(count, 0)}件を比較しています。`;

  const legacyHorizons = [1, 5, 20].map((days) => ({
    days,
    observations: context.similar_move_count,
    winRate: context[`forward_win_rate_${days}d`],
    mean: context[`forward_mean_${days}d`],
    median: context[`forward_median_${days}d`],
  }));
  const horizons = (
    Array.isArray(context.forward_path) && context.forward_path.length
      ? context.forward_path.map((point) => ({
          days: Number(point.horizon),
          observations: Number(point.observations || 0),
          winRate: point.win_rate,
          mean: point.mean,
          median: point.median,
          stddev: point.stddev,
          q1: point.q1,
          q3: point.q3,
        }))
      : legacyHorizons
  ).filter((item) => item.winRate != null);
  if (!horizons.length) return;

  const left = 64;
  const right = 18;
  const top = 30;
  const bottom = 48;
  const width = 720 - left - right;
  const height = 300 - top - bottom;
  const y = (value) => top + (1 - value) * height;
  [0, 0.25, 0.5, 0.75, 1].forEach((value) => {
    const gridY = y(value);
    elements.analogChart.append(
      svgNode("line", {
        x1: left,
        y1: gridY,
        x2: left + width,
        y2: gridY,
        class: value === 0.5 ? "report-chart-chance" : "report-chart-grid",
      }),
      svgNode("text", {
        x: left - 9,
        y: gridY + 4,
        "text-anchor": "end",
        class: "report-chart-label",
      }, `${Math.round(value * 100)}%`),
    );
  });

  const x = (days) => left + ((days - 1) / 19) * width;
  const continuous = horizons.length === 20 && horizons.every(
    (item, index) => item.days === index + 1,
  );
  if (continuous) {
    const path = horizons.map((item, index) =>
      `${index ? "L" : "M"}${x(item.days)},${y(Number(item.winRate))}`,
    ).join(" ");
    elements.analogChart.append(svgNode("path", {
      d: path,
      class: "report-chart-analog-line",
      opacity: count < 20 ? 0.42 : 1,
    }));
  }
  horizons.forEach((item) => {
    const pointX = x(item.days);
    const pointY = y(Number(item.winRate));
    const point = svgNode("circle", {
      cx: pointX,
      cy: pointY,
      r: 4.5,
      class: Number(item.winRate) >= 0.5
        ? "report-chart-analog-positive"
        : "report-chart-analog-negative",
      opacity: count < 20 ? 0.42 : 1,
    });
    point.append(svgNode("title", {},
      `${item.days}日後 上昇率${percent(item.winRate)} / ` +
      `平均${percent(item.mean, true)} / 中央値${percent(item.median, true)} / ` +
      `${number(item.observations, 0)}観測`,
    ));
    elements.analogChart.append(point);
    if ([1, 5, 10, 15, 20].includes(item.days)) {
      elements.analogChart.append(
        svgNode("text", {
          x: pointX,
          y: pointY + (Number(item.winRate) >= 0.5 ? -10 : 17),
          "text-anchor": "middle",
          class: "report-chart-value",
        }, percent(item.winRate)),
        svgNode("text", {
        x: pointX,
        y: y(0) + 20,
        "text-anchor": "middle",
        class: "report-chart-label",
        }, `${item.days}日`),
      );
    }
  });

  const best = horizons.reduce((highest, item) =>
    Number(item.winRate) > Number(highest.winRate) ? item : highest,
  );
  const expectedCandidates = horizons
    .filter((item) => item.mean != null && Number(item.observations) >= 20)
    .map((item) => {
      const stddev = Number(item.stddev);
      // Forward windows overlap (especially at 20D), so treating every daily
      // starting point as independent would make uncertainty look too small.
      // n / horizon is a deliberately conservative effective sample size.
      const effectiveObservations = Math.max(1, item.observations / item.days);
      const standardError = Number.isFinite(stddev) && item.observations > 1
        ? stddev / Math.sqrt(effectiveObservations)
        : null;
      return {
        ...item,
        effectiveObservations,
        // One-sided 80% lower confidence bound. This deliberately penalises
        // a high sample mean when its historical outcomes are widely spread.
        conservativeMean: standardError == null
          ? null
          : Number(item.mean) - 1.2816 * standardError,
      };
    });
  const bestExpected = expectedCandidates.reduce(
    (highest, item) => highest == null || Number(item.mean) > Number(highest.mean)
      ? item
      : highest,
    null,
  );
  const bestConservative = expectedCandidates
    .filter((item) => item.conservativeMean != null)
    .reduce(
      (highest, item) => highest == null || item.conservativeMean > highest.conservativeMean
        ? item
        : highest,
      null,
    );
  if (bestExpected) {
    const threshold = `${percent(move, true)}${move < 0 ? "以下" : "以上"}`;
    const heading = document.createElement("strong");
    heading.textContent = `${threshold}動いた後の期待リターン最大：${bestExpected.days}日後`;
    const detail = document.createElement("p");
    detail.textContent =
      `平均${percent(bestExpected.mean, true)}、中央値${percent(bestExpected.median, true)}、` +
      `中央50% ${percent(bestExpected.q1, true)}〜${percent(bestExpected.q3, true)}、` +
      `上昇率${percent(bestExpected.winRate)}、n=${number(bestExpected.observations, 0)}。`;
    const caution = document.createElement("p");
    if (bestConservative) {
      const positive = bestConservative.conservativeMean > 0;
      caution.textContent =
        `ばらつきを差し引いた保守的な候補は${bestConservative.days}日後` +
        `（80%片側下限 ${percent(bestConservative.conservativeMean, true)}）です。` +
        `${positive ? "下限もプラスです。" : "下限はマイナスのため、優位性はまだ確実ではありません。"}`;
    } else {
      caution.textContent = "ばらつきを評価するための観測数が不足しています。";
    }
    elements.analogDecision.append(heading, detail, caution);
    elements.analogDecision.hidden = false;
  }
  const day5 = horizons.find((item) => item.days === 5);
  const sampleWarning = count < 20
    ? `過去${count}件のみなので参考値です。`
    : `過去${count}件を使っています。`;
  const day5Read = day5
    ? `5日後は上昇率${percent(day5.winRate)}、中央値${percent(day5.median, true)}です。`
    : "";
  elements.analogInterpretation.textContent =
    `今回：${day5Read}最も上昇率が高いのは${best.days}日後の${percent(best.winRate)}です。` +
    `${sampleWarning}平均リターンは利益・損失と発生回数をすでに反映した期待値です。` +
    `保守候補は期間の重複を考慮した標準誤差を差し引いて選びます。` +
    `これはニュース内容ではなく、値動きだけを条件にした統計です。`;
}

function averageFinite(values) {
  const finite = values.map(Number).filter(Number.isFinite);
  return finite.length
    ? finite.reduce((sum, value) => sum + value, 0) / finite.length
    : null;
}

function pearsonCorrelation(left, right) {
  const pairs = left.map((value, index) => [Number(value), Number(right[index])])
    .filter(([a, b]) => Number.isFinite(a) && Number.isFinite(b));
  if (pairs.length < 5) return null;
  const leftMean = averageFinite(pairs.map(([value]) => value));
  const rightMean = averageFinite(pairs.map(([, value]) => value));
  const numerator = pairs.reduce(
    (sum, [a, b]) => sum + (a - leftMean) * (b - rightMean),
    0,
  );
  const leftScale = Math.sqrt(pairs.reduce(
    (sum, [a]) => sum + (a - leftMean) ** 2,
    0,
  ));
  const rightScale = Math.sqrt(pairs.reduce(
    (sum, [, b]) => sum + (b - rightMean) ** 2,
    0,
  ));
  return leftScale && rightScale ? numerator / (leftScale * rightScale) : null;
}

function surfaceRegime(cells) {
  const edge = (from, to) => averageFinite(cells
    .filter((cell) => cell.horizon >= from && cell.horizon <= to)
    .map((cell) => cell.conditionalEdge));
  const early = edge(1, 5);
  const middle = edge(6, 13);
  const late = edge(14, 20);
  const threshold = 0.001;
  let label = "方向不安定型";
  let reading = "期間によって通常平均との差が入れ替わり、単純な方向判断には向きません。";
  if (early < -threshold && late > threshold) {
    label = "短期調整後の回復型";
    reading = "短期は通常より弱く、その後に相対的な回復が表れています。";
  } else if (early > threshold && late < -threshold) {
    label = "初動優位後の失速型";
    reading = "短期の優位が後半に失われるため、長期保有には慎重さが必要です。";
  } else if ([early, middle, late].every((value) => value > threshold)) {
    label = "継続優位型";
    reading = "全期間で通常平均を上回り、相対的な強さが継続しています。";
  } else if ([early, middle, late].every((value) => value < -threshold)) {
    label = "継続劣位型";
    reading = "全期間で通常平均を下回り、反発より弱さの継続を警戒する帯です。";
  } else if (late != null && early != null && late - early > threshold * 2) {
    label = "改善型";
    reading = "日数の経過とともに通常平均との差が改善しています。";
  } else if (late != null && early != null && early - late > threshold * 2) {
    label = "悪化型";
    reading = "日数の経過とともに通常平均との差が悪化しています。";
  }
  return { label, reading, early, middle, late };
}

function surfaceStability(bucket, buckets, byCell, planByBucket) {
  const selected = Array.from({ length: 20 }, (_, index) =>
    byCell.get(`${bucket}:${index + 1}`)?.conditionalEdge,
  );
  const adjacent = buckets.filter((candidate) => Math.abs(candidate - bucket) === 1);
  const correlations = adjacent.map((candidate) => pearsonCorrelation(
    selected,
    Array.from({ length: 20 }, (_, index) =>
      byCell.get(`${candidate}:${index + 1}`)?.conditionalEdge,
    ),
  )).filter(Number.isFinite);
  const correlation = averageFinite(correlations);
  const plan = planByBucket.get(bucket);
  const comparablePlans = adjacent.map((candidate) => planByBucket.get(candidate)).filter(Boolean);
  const matchingPlans = plan
    ? comparablePlans.filter((candidate) =>
      Math.abs(Number(candidate.buy_day) - Number(plan.buy_day)) <= 3 &&
      Math.abs(Number(candidate.sell_day) - Number(plan.sell_day)) <= 3,
    ).length
    : 0;
  let label = "低い";
  if (correlation >= 0.65 && matchingPlans === comparablePlans.length && comparablePlans.length) {
    label = "高い";
  } else if (correlation >= 0.3 || matchingPlans > 0) {
    label = "中程度";
  }
  return { label, correlation, matchingPlans, comparablePlans: comparablePlans.length };
}

function renderReturnPath(cells, plan) {
  const svg = elements.returnPathChart;
  svg.replaceChildren();
  if (!cells.length) return;
  const width = 720;
  const height = 300;
  const left = 66;
  const right = 20;
  const top = 42;
  const bottom = 46;
  const plotWidth = width - left - right;
  const plotHeight = height - top - bottom;
  const values = [0, ...cells.flatMap((cell) => [
    Number(cell.forward_mean),
    Number(cell.baselineMean),
  ]).filter(Number.isFinite)];
  const rawMin = Math.min(...values);
  const rawMax = Math.max(...values);
  const span = Math.max(rawMax - rawMin, 0.01);
  const minimum = rawMin - span * 0.15;
  const maximum = rawMax + span * 0.15;
  const x = (horizon) => left + ((horizon - 1) / 19) * plotWidth;
  const y = (value) => top + ((maximum - value) / (maximum - minimum)) * plotHeight;

  for (let index = 0; index <= 4; index += 1) {
    const value = maximum - ((maximum - minimum) * index) / 4;
    const pointY = y(value);
    svg.append(
      svgNode("line", {
        x1: left,
        y1: pointY,
        x2: width - right,
        y2: pointY,
        class: Math.abs(value) < span / 12 ? "report-chart-zero" : "report-chart-grid",
      }),
      svgNode("text", {
        x: left - 10,
        y: pointY + 4,
        "text-anchor": "end",
        class: "report-chart-label",
      }, percent(value, true)),
    );
  }
  [1, 5, 10, 15, 20].forEach((horizon) => {
    svg.append(svgNode("text", {
      x: x(horizon),
      y: height - 14,
      "text-anchor": "middle",
      class: "report-chart-label",
    }, `${horizon}日`));
  });
  const pathFor = (key) => cells.map((cell, index) =>
    `${index ? "L" : "M"}${x(cell.horizon).toFixed(1)},${y(Number(cell[key])).toFixed(1)}`,
  ).join(" ");
  svg.append(
    svgNode("path", { d: pathFor("baselineMean"), class: "report-surface-path-baseline" }),
    svgNode("path", { d: pathFor("forward_mean"), class: "report-surface-path-expected" }),
    svgNode("text", { x: left, y: 20, class: "report-chart-label" }, "青: 選択帯の絶対期待値"),
    svgNode("text", { x: left + 180, y: 20, class: "report-chart-label" }, "破線: 同期間の通常平均"),
  );
  if (!plan) return;
  [["B", Number(plan.buy_day), "report-surface-path-buy"],
    ["S", Number(plan.sell_day), "report-surface-path-sell"]].forEach(([label, horizon, className]) => {
    const cell = cells.find((item) => item.horizon === horizon);
    if (!cell) return;
    const markerX = x(horizon);
    const markerY = y(Number(cell.forward_mean));
    svg.append(
      svgNode("line", {
        x1: markerX,
        y1: top,
        x2: markerX,
        y2: top + plotHeight,
        class: `report-surface-path-marker ${className}`,
      }),
      svgNode("circle", { cx: markerX, cy: markerY, r: 6, class: className }),
      svgNode("text", {
        x: markerX,
        y: top - 8,
        "text-anchor": "middle",
        class: "report-chart-value",
      }, `${label} ${horizon}日`),
    );
  });
}

function renderReturnSurface(rows, tradePlans) {
  const svg = elements.returnSurfaceChart;
  svg.replaceChildren();
  elements.returnPathChart.replaceChildren();
  elements.returnSurfaceBucket.replaceChildren();
  elements.returnSurfaceSummary.textContent = "";
  elements.returnSurfaceDecision.replaceChildren();
  elements.returnSurfaceDecision.hidden = true;
  elements.returnSurfaceInterpretation.textContent = "算出可能な日足履歴がありません。";
  if (!Array.isArray(rows) || !rows.length) return;

  const buckets = [...new Set(rows.map((row) => Number(row.move_bucket)))].sort(
    (left, right) => left - right,
  );
  const byCell = new Map();
  rows.forEach((row) => {
    const observations = Number(row.observations || 0);
    const horizon = Number(row.horizon);
    const stddev = Number(row.forward_stddev);
    const suppliedEffective = Number(row.effective_observations);
    const effective = Number.isFinite(suppliedEffective)
      ? suppliedEffective
      : Math.max(1, observations / horizon);
    const standardError = Number.isFinite(stddev) && observations > 1
      ? stddev / Math.sqrt(effective)
      : null;
    byCell.set(`${row.move_bucket}:${horizon}`, {
      ...row,
      horizon,
      observations,
      effective,
      conservativeLow: standardError == null
        ? null
        : Number(row.forward_mean) - 1.2816 * standardError,
      conservativeHigh: standardError == null
        ? null
        : Number(row.forward_mean) + 1.2816 * standardError,
    });
  });

  const eligible = [...byCell.values()].filter(
    (cell) => cell.effective >= 10 && cell.forward_mean != null,
  );
  if (!eligible.length) {
    elements.returnSurfaceInterpretation.textContent =
      "期間重複を補正した実効標本が10件以上の領域はまだありません。";
    return;
  }

  const smoothed = rows.some((row) => row.surface_method != null);
  const baselineByHorizon = new Map();
  for (let horizon = 1; horizon <= 20; horizon += 1) {
    const cells = [...byCell.values()].filter(
      (cell) => cell.horizon === horizon && cell.forward_mean != null,
    );
    const total = cells.reduce((sum, cell) => sum + cell.observations, 0);
    baselineByHorizon.set(
      horizon,
      total
        ? cells.reduce(
            (sum, cell) => sum + Number(cell.forward_mean) * cell.observations,
            0,
          ) / total
        : 0,
    );
  }
  [...byCell.values()].forEach((cell) => {
    if (cell.forward_mean == null) return;
    const suppliedBaseline = Number(cell.baseline_mean);
    const suppliedEdge = Number(cell.conditional_edge);
    cell.baselineMean = Number.isFinite(suppliedBaseline)
      ? suppliedBaseline
      : baselineByHorizon.get(cell.horizon) || 0;
    cell.conditionalEdge = Number.isFinite(suppliedEdge)
      ? suppliedEdge
      : Number(cell.forward_mean) - cell.baselineMean;
  });
  const scale = Math.max(
    quantile(eligible.map((cell) => Math.abs(cell.conditionalEdge)), 0.9) || 0,
    0.005,
  );
  const left = 92;
  const top = 52;
  const plotWidth = 780;
  const plotHeight = 360;
  const cellWidth = plotWidth / buckets.length;
  const cellHeight = plotHeight / 20;
  const planByBucket = new Map((tradePlans || []).map(
    (plan) => [Number(plan.move_bucket), plan],
  ));

  svg.append(
    svgNode("text", { x: left, y: 22, class: "report-chart-label" }, "同期間の通常平均との差"),
    svgNode("rect", { x: left + 82, y: 11, width: 42, height: 13, fill: "rgba(239,68,68,.65)" }),
    svgNode("text", { x: left + 130, y: 22, class: "report-chart-label" }, "低い"),
    svgNode("rect", { x: left + 174, y: 11, width: 42, height: 13, fill: "rgba(34,197,94,.65)" }),
    svgNode("text", { x: left + 222, y: 22, class: "report-chart-label" }, "高い"),
    svgNode("text", { x: left + 285, y: 22, class: "report-chart-label" }, "B→S リスク補正後の最適売買ペア"),
  );

  for (let horizon = 1; horizon <= 20; horizon += 1) {
    const y = top + (horizon - 1) * cellHeight;
    svg.append(svgNode("text", {
      x: left - 12,
      y: y + cellHeight * 0.72,
      "text-anchor": "end",
      class: "report-chart-label",
    }, `${horizon}日`));
    buckets.forEach((bucket, index) => {
      const x = left + index * cellWidth;
      const cell = byCell.get(`${bucket}:${horizon}`);
      const reliable = cell && cell.effective >= 10 && cell.forward_mean != null;
      const value = reliable ? cell.conditionalEdge : 0;
      const intensity = reliable ? Math.min(Math.abs(value) / scale, 1) : 0;
      const rect = svgNode("rect", {
        x: x + 1,
        y: y + 1,
        width: Math.max(cellWidth - 2, 1),
        height: Math.max(cellHeight - 2, 1),
        rx: 2,
        fill: reliable
          ? value >= 0
            ? `rgba(34,197,94,${0.15 + intensity * 0.75})`
            : `rgba(239,68,68,${0.15 + intensity * 0.75})`
          : "rgba(148,163,184,.05)",
        stroke: "rgba(148,163,184,.16)",
        class: "report-surface-cell",
        tabindex: 0,
        role: "button",
        "aria-label": `変動帯${bucket}、${horizon}日後を選択`,
      });
      const chooseBucket = () => {
        elements.returnSurfaceBucket.value = String(bucket);
        updateSelection(bucket);
      };
      rect.addEventListener("click", chooseBucket);
      rect.addEventListener("keydown", (event) => {
        if (event.key === "Enter" || event.key === " ") chooseBucket();
      });
      if (cell) {
        const range = cell.surface_method === "kernel"
          ? `初日${percent(cell.target_move, true)}近傍（帯域幅${percent(cell.bandwidth)}）`
          : `${percent(cell.move_min, true)}〜${percent(cell.move_max, true)}`;
        rect.append(svgNode("title", {}, reliable
          ? `${range} / ${horizon}日後 / 絶対期待値${percent(cell.forward_mean, true)} / ` +
            `通常平均${percent(cell.baselineMean, true)} / 差${percent(cell.conditionalEdge, true)} / ` +
            `中央値${percent(cell.forward_median, true)} / 上昇率${percent(cell.forward_win_rate)} / ` +
            `80%下限${percent(cell.conservativeLow, true)} / ` +
            `n=${cell.observations} / 重複補正後n≈${number(cell.effective, 1)}`
          : `${range} / ${horizon}日後 / 実効標本不足`,
        ));
      }
      svg.append(rect);
      if (!reliable) return;
      const plan = planByBucket.get(bucket);
      const sell = Number(plan?.sell_day) === horizon;
      const buy = Number(plan?.buy_day) === horizon;
      if (sell || buy) {
        const marker = sell && buy ? "S/B" : sell ? "S" : "B";
        const markerNode = svgNode("text", {
          x: x + cellWidth / 2,
          y: y + cellHeight * 0.72,
          "text-anchor": "middle",
          class: "report-surface-marker",
        }, marker);
        markerNode.append(svgNode("title", {},
          `B ${plan.buy_day}日後 → S ${plan.sell_day}日後 / ` +
          `期待${percent(plan.expected_return_after_cost, true)} / ` +
          `勝率${percent(plan.win_rate)} / 10%点${percent(plan.downside_p10_after_cost, true)} / ` +
          `80%下限${percent(plan.conservative_return, true)}`,
        ));
        svg.append(markerNode);
      }
    });
  }

  buckets.forEach((bucket, index) => {
    const sample = rows.find((row) => Number(row.move_bucket) === bucket);
    const x = left + (index + 0.5) * cellWidth;
    if (smoothed) {
      const label = sample?.surface_method === "lower_tail"
        ? `≤${percent(sample.target_move, true)}`
        : sample?.surface_method === "upper_tail"
          ? `≥${percent(sample.target_move, true)}`
          : percent(sample?.target_move, true);
      svg.append(svgNode("text", {
        x,
        y: top + plotHeight + 24,
        "text-anchor": "middle",
        class: "report-chart-sample",
      }, label));
    } else {
      svg.append(
        svgNode("text", {
          x,
          y: top + plotHeight + 22,
          "text-anchor": "middle",
          class: "report-chart-label",
        }, `平均${percent(sample?.move_mean, true)}`),
        svgNode("text", {
          x,
          y: top + plotHeight + 40,
          "text-anchor": "middle",
          class: "report-chart-sample",
        }, `${percent(sample?.move_min, true)}〜`),
        svgNode("text", {
          x,
          y: top + plotHeight + 55,
          "text-anchor": "middle",
          class: "report-chart-sample",
        }, percent(sample?.move_max, true)),
      );
    }
  });
  svg.append(svgNode("text", {
    x: left + plotWidth / 2,
    y: 505,
    "text-anchor": "middle",
    class: "report-chart-label",
  }, smoothed
    ? "初日の1日リターン（中央90%は連続平滑化、両端は上下5%）"
    : "初日の1日リターン（同銘柄内10分位）"));

  const selectionOutline = svgNode("rect", {
    x: left + 1,
    y: top + 1,
    width: Math.max(cellWidth - 2, 1),
    height: plotHeight - 2,
    rx: 3,
    class: "report-surface-selection",
  });
  svg.append(selectionOutline);

  const samplesByBucket = new Map(buckets.map((bucket) => [
    bucket,
    rows.find((row) => Number(row.move_bucket) === bucket),
  ]));
  buckets.forEach((bucket) => {
    const sample = samplesByBucket.get(bucket);
    const option = document.createElement("option");
    option.value = String(bucket);
    option.textContent = sample?.surface_method === "lower_tail"
      ? `急落帯 ${percent(sample.move_min, true)}〜${percent(sample.move_max, true)}`
      : sample?.surface_method === "upper_tail"
        ? `急騰帯 ${percent(sample.move_min, true)}〜${percent(sample.move_max, true)}`
        : sample?.surface_method === "kernel"
          ? `初日 ${percent(sample.target_move, true)}近傍`
          : `平均 ${percent(sample?.move_mean, true)}（${percent(sample?.move_min, true)}〜` +
            `${percent(sample?.move_max, true)}）`;
    elements.returnSurfaceBucket.append(option);
  });

  function updateSelection(bucket) {
    const index = buckets.indexOf(bucket);
    if (index < 0) return;
    selectionOutline.setAttribute("x", String(left + index * cellWidth + 1));
    const selectedCells = Array.from({ length: 20 }, (_, horizonIndex) =>
      byCell.get(`${bucket}:${horizonIndex + 1}`),
    ).filter((cell) => cell?.forward_mean != null);
    const plan = planByBucket.get(bucket);
    const sample = samplesByBucket.get(bucket);
    const regime = surfaceRegime(selectedCells);
    const stability = surfaceStability(bucket, buckets, byCell, planByBucket);
    const correlationText = stability.correlation == null
      ? "算出不可"
      : stability.correlation.toFixed(2);
    const bandDescription = sample?.surface_method === "kernel"
      ? `初日${percent(sample.target_move, true)}近傍（帯域幅${percent(sample.bandwidth)}）`
      : `${percent(sample?.move_min, true)}〜${percent(sample?.move_max, true)}の初日変動`;
    elements.returnSurfaceSummary.textContent =
      `${bandDescription}：` +
      `${regime.label}。${regime.reading} 隣接帯との安定性は${stability.label}` +
      `（経路相関 ${correlationText}、近いB/S ${stability.matchingPlans}/` +
      `${stability.comparablePlans}帯）です。`;
    renderReturnPath(selectedCells, plan);
    elements.returnSurfaceDecision.replaceChildren();
    elements.returnSurfaceDecision.hidden = !plan;
    if (!plan) {
      elements.returnSurfaceInterpretation.textContent =
        "この帯は実効標本10件以上の売買ペアがなく、B/Sを表示していません。";
      return;
    }
    const evidenceLabels = {
      strong: "強い（80%信頼下限もプラス）",
      moderate: "中程度（平均と勝率はプラス）",
      weak: "弱い（下振れを考慮すると優位性未確認）",
      insufficient: "標本不足",
    };
    const heading = document.createElement("strong");
    heading.textContent = `${regime.label}の売買候補`;
    const timing = document.createElement("p");
    timing.textContent =
      `推奨待機 ${plan.buy_day}日 → ${plan.holding_days}日間保有 → ` +
      `${plan.sell_day}日後に売却${plan.sell_at_window_boundary ? "（観測期間末）" : ""}`;
    const expected = document.createElement("p");
    expected.textContent =
      `コスト後期待リターン ${percent(plan.expected_return_after_cost, true)} / ` +
      `勝率 ${percent(plan.win_rate)} / 下振れ10%点 ` +
      `${percent(plan.downside_p10_after_cost, true)}`;
    const evidence = document.createElement("p");
    evidence.textContent =
      `同subsector超過 ${percent(plan.sector_excess_return, true)} / ` +
      `80%信頼下限 ${percent(plan.conservative_return, true)} / ` +
      `実効標本 ${number(plan.effective_observations, 1)} / ` +
      `ペア内判定 ${evidenceLabels[plan.evidence_level] || "—"}`;
    const caution = document.createElement("p");
    caution.className = "report-decision-caution";
    caution.textContent =
      "多数のB→S候補から最良値を選んだ探索結果です。隣接帯の安定性と、下段の未使用期間における検証実績を確認してから判断します。";
    elements.returnSurfaceDecision.append(heading, timing, expected, evidence, caution);
    elements.returnSurfaceInterpretation.textContent = plan.sell_at_window_boundary
      ? "Sが20日後にあるためピークは未確認です。20日後を機械的な売却日とはせず、観測窓を延ばして再検証します。"
      : `選択帯は${regime.label}です。折れ線の青と破線の間隔、BからSまでの経路、下振れ10%点を順に確認します。`;
  }

  const initialBucket = [...buckets].reverse().find((bucket) => planByBucket.has(bucket))
    ?? buckets.at(-1);
  elements.returnSurfaceBucket.value = String(initialBucket);
  elements.returnSurfaceBucket.onchange = () =>
    updateSelection(Number(elements.returnSurfaceBucket.value));
  updateSelection(initialBucket);
}

function validationCondition(result) {
  if (result.surface_method === "lower_tail") {
    return `急落 ${percent(result.move_min, true)}〜${percent(result.move_max, true)}`;
  }
  if (result.surface_method === "upper_tail") {
    return `急騰 ${percent(result.move_min, true)}〜${percent(result.move_max, true)}`;
  }
  return `${percent(result.target_move, true)}近傍（±${percent(result.bandwidth)}）`;
}

function renderWalkForward(report, symbol) {
  const results = report.walk_forward_simulations?.[symbol] || [];
  const examples = report.walk_forward_examples?.[symbol] || [];
  elements.walkForwardBody.replaceChildren();
  elements.walkForwardExampleBody.replaceChildren();
  if (!results.length) {
    elements.walkForwardSummary.textContent =
      "時系列分割後に十分な学習・検証標本を確保できませんでした。";
    return;
  }

  const weightedObservations = results.reduce(
    (sum, result) => sum + Number(result.validation_observations || 0),
    0,
  );
  const weightedWinRate = weightedObservations
    ? results.reduce(
        (sum, result) => sum
          + Number(result.actual_win_rate || 0)
          * Number(result.validation_observations || 0),
        0,
      ) / weightedObservations
    : null;
  const directionAccuracy = results.filter((result) => result.direction_correct).length
    / results.length;
  const calibrationMae = results.reduce(
    (sum, result) => sum + Math.abs(Number(result.calibration_error || 0)),
    0,
  ) / results.length;
  const latestFold = Math.max(...results.map((result) => Number(result.fold)));
  const latest = results
    .filter((result) => Number(result.fold) === latestFold)
    .sort((left, right) => Number(left.move_bucket) - Number(right.move_bucket));
  const latestFrom = latest.map((result) => result.validation_from).sort()[0];
  const latestThrough = latest.map((result) => result.validation_through).sort().at(-1);
  elements.walkForwardSummary.textContent =
    `${results.length}条件区間・延べ${number(weightedObservations, 0)}シグナルを将来側で検証。` +
    `期待方向の一致率${percent(directionAccuracy)}、期待値の平均絶対誤差` +
    `${percent(calibrationMae)}、シグナル勝率${percent(weightedWinRate)}です。` +
    `表は直近の未使用期間（${latestFrom}〜${latestThrough}）を示します。`;

  latest.forEach((result) => {
    row(elements.walkForwardBody, [
      validationCondition(result),
      `${result.validation_from}〜${result.validation_through}`,
      `+${result.buy_day}日買い → +${result.sell_day}日売り`,
      percent(result.expected_return_after_cost, true),
      percent(result.actual_mean_return, true),
      percent(result.actual_win_rate),
      `${number(result.validation_observations, 0)}（実効${number(result.validation_effective_observations, 1)}）`,
      percent(result.calibration_error, true),
    ]);
  });

  examples
    .filter((example) => Number(example.fold) === latestFold)
    .sort((left, right) => String(right.signal_date).localeCompare(String(left.signal_date)))
    .slice(0, 10)
    .forEach((example) => {
      row(elements.walkForwardExampleBody, [
        example.signal_date,
        percent(example.actual_initial_move, true),
        `+${example.buy_day}日買い → +${example.sell_day}日売り`,
        percent(example.actual_return_after_cost, true),
      ]);
    });
}

function renderHorizonChart(studies) {
  const svg = elements.focusHorizonChart;
  svg.replaceChildren();
  elements.focusHorizonInterpretation.textContent = "算出可能な観測がありません。";
  const points = HORIZONS.map((horizon) => ({
    horizon,
    ...focusStatistics(studies, horizon),
  })).map((point) => ({
    ...point,
    minimum: point.reactionReturns.length ? Math.min(...point.reactionReturns) : null,
    q1: quantile(point.reactionReturns, 0.25),
    q3: quantile(point.reactionReturns, 0.75),
    maximum: point.reactionReturns.length ? Math.max(...point.reactionReturns) : null,
  }));
  const values = points.flatMap((point) => point.reactionReturns);
  if (!values.length) return;
  const geometry = chartGeometry(values);
  drawChartGrid(svg, geometry);
  const x = (horizon) => {
    const index = HORIZONS.indexOf(horizon);
    return geometry.left + (index / (HORIZONS.length - 1)) * geometry.width;
  };

  points.filter((point) => point.dateEvents).forEach((point) => {
    const pointX = x(point.horizon);
    const opacity = point.dateEvents < 5 ? 0.42 : 1;
    const rangeTitle = `${horizonLabel(point.horizon)} / 中央値${percent(point.median, true)} / ` +
      `中央50% ${percent(point.q1, true)}〜${percent(point.q3, true)} / n=${point.dateEvents}`;
    const range = svgNode("line", {
      x1: pointX,
      y1: geometry.y(point.maximum),
      x2: pointX,
      y2: geometry.y(point.minimum),
      class: "report-chart-range",
      opacity,
    });
    range.append(svgNode("title", {}, rangeTitle));
    const iqr = svgNode("line", {
      x1: pointX,
      y1: geometry.y(point.q3),
      x2: pointX,
      y2: geometry.y(point.q1),
      class: "report-chart-iqr",
      opacity,
    });
    iqr.append(svgNode("title", {}, rangeTitle));
    const medianPoint = svgNode("circle", {
      cx: pointX,
      cy: geometry.y(point.median),
      r: 6,
      class: "report-chart-median-point",
      opacity,
    });
    medianPoint.append(svgNode("title", {}, rangeTitle));
    svg.append(range, iqr, medianPoint, svgNode(
      "text",
      {
        x: pointX,
        y: geometry.y(point.median) + (point.median >= 0 ? -11 : 19),
        "text-anchor": "middle",
        class: "report-chart-value",
        opacity,
      },
      percent(point.median, true),
    ));
  });

  svg.append(
    svgNode("line", { x1: geometry.left, y1: 18, x2: geometry.left + 22, y2: 18, class: "report-chart-range" }),
    svgNode("text", { x: geometry.left + 28, y: 22, class: "report-chart-label" }, "観測全範囲"),
    svgNode("line", { x1: geometry.left + 140, y1: 18, x2: geometry.left + 162, y2: 18, class: "report-chart-iqr" }),
    svgNode("text", { x: geometry.left + 170, y: 22, class: "report-chart-label" }, "中央50%"),
    svgNode("circle", { cx: geometry.left + 276, cy: 18, r: 6, class: "report-chart-median-point" }),
    svgNode("text", { x: geometry.left + 288, y: 22, class: "report-chart-label" }, "中央値"),
  );

  HORIZONS.forEach((horizon) => {
    const horizonPoint = points.find((point) => point.horizon === horizon);
    const sampleLabel = horizonPoint?.dateEvents < 5
      ? `参考 n=${horizonPoint?.dateEvents || 0}`
      : `n=${horizonPoint?.dateEvents || 0} / 上昇${percent(horizonPoint?.reactionWinRate)}`;
    svg.append(
      svgNode("text",
      {
        x: x(horizon),
        y: geometry.top + geometry.height + 20,
        "text-anchor": "middle",
        class: "report-chart-label",
      },
      horizon === 0 ? "反応日" : `+${horizon}日`,
      ),
      svgNode("text", {
        x: x(horizon),
        y: geometry.top + geometry.height + 38,
        "text-anchor": "middle",
        class: "report-chart-sample",
      }, sampleLabel),
    );
  });

  const day0 = points.find((point) => point.horizon === 0);
  const day5 = points.find((point) => point.horizon === 5);
  const day20 = points.find((point) => point.horizon === 20);
  const trend = day5?.median == null
    ? "5日後までの方向はまだ判定できません。"
    : day5.median < 0
      ? `5日後中央値は${percent(day5.median, true)}で、過去事例は短期的に下落側へ偏っています。`
      : `5日後中央値は${percent(day5.median, true)}で、過去事例は短期的に上昇側へ偏っています。`;
  const movement = day0?.median != null && day5?.median != null
    ? `反応日${percent(day0.median, true)}から5日後${percent(day5.median, true)}へ変化しました。`
    : "";
  const consistency = day5?.q1 < 0 && day5?.q3 < 0
    ? "5日後の中央50%もすべてマイナスで、下落傾向に一定の再現性があります。"
    : day5?.q1 < 0 && day5?.q3 > 0
      ? "5日後の中央50%が0%をまたぎ、結果は上昇と下落に割れています。"
      : day5?.q1 > 0
        ? "5日後の中央50%もすべてプラスで、上昇傾向に一定の再現性があります。"
        : "";
  const longTerm = !day20 || day20.dateEvents < 5
    ? `20日後は${day20?.dateEvents || 0}反応日しかなく、長期判断には使えません。`
    : `20日後は${day20.dateEvents}反応日の観測があります。`;
  elements.focusHorizonInterpretation.textContent =
    `今回：${movement}${trend}${consistency}${longTerm} 単独の売買シグナルではなく、価格トレンドと合わせて使います。`;
}

function renderTimelineChart(studies) {
  const svg = elements.focusTimelineChart;
  svg.replaceChildren();
  elements.focusTimelineInterpretation.textContent = "算出可能な反応日がありません。";
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

  const strongest = points.reduce((best, point) =>
    Math.abs(point.value) > Math.abs(best.value) ? point : best,
  );
  const highVolume = points.filter((point) => point.volume >= 1.5).length;
  const positive = points.filter((point) => point.value > 0).length;
  elements.focusTimelineInterpretation.textContent =
    `今回：最大変動は${strongest.date}の${percent(strongest.value, true)}です。` +
    `${points.length}反応日のうち上昇は${positive}日、出来高1.5倍以上は${highVolume}日でした。` +
    "大きな棒が一方向に継続するかを確認し、単発なら個別ケースとして扱います。";
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
  renderAnalogChart(studies);
  const smoothedSurface = report.smoothed_return_surfaces?.[symbol] || [];
  const smoothedPlans = report.smoothed_trade_plans?.[symbol] || [];
  renderWalkForward(report, symbol);
  renderReturnSurface(
    smoothedSurface.length
      ? smoothedSurface
      : report.return_surfaces?.[symbol] || [],
    smoothedSurface.length
      ? smoothedPlans
      : report.return_trade_plans?.[symbol] || [],
  );
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
  const reportUrl = new URL(
    `/api/analysis/reports/${encodeURIComponent(reportDate)}`,
    window.location.origin,
  );
  reportUrl.searchParams.set("fresh", Date.now().toString());
  const response = await fetch(reportUrl, {
    cache: "no-store",
    headers: { Accept: "application/json", "Cache-Control": "no-cache" },
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
    const indexUrl = new URL("/api/analysis/reports", window.location.origin);
    indexUrl.searchParams.set("fresh", Date.now().toString());
    const response = await fetch(indexUrl, {
      cache: "no-store",
      headers: { Accept: "application/json", "Cache-Control": "no-cache" },
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

elements.analogEvent.addEventListener("change", () => {
  if (!activeReport) return;
  const studies = (activeReport.case_studies || []).filter(
    (study) => study.symbol === elements.focusSymbol.value,
  );
  renderAnalogChart(studies, elements.analogEvent.value);
});

start();
