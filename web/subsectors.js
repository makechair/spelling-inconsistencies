// Japanese labels for the universe's subsector slugs.
//
// One label per slug and no label used twice: two headings that read the same
// would make the filter ambiguous in exactly the place it is meant to
// disambiguate. The slugs stay the identifiers everywhere else -- in the CSVs,
// the Parquet and the API -- because renaming them would orphan the corpus.

export const SUBSECTOR_LABELS = {
  logic_compute: "ロジック・演算",
  memory_storage: "メモリ・ストレージ",
  foundry: "ファウンドリ",
  equipment: "製造装置",
  eda_ip: "EDA・IP",
  materials: "材料・部材",
  analog_power_rf: "アナログ・パワー・RF",
  ai_platform: "AIプラットフォーム",
  ai_systems_networking: "AIシステム・ネットワーク",
  emerging_silicon: "新興半導体",
  components: "電子部品",
  optical_interconnect: "光通信・インターコネクト",
  diversified: "総合電機",
  // Companies the user added from the page. The EDINET code list carries an
  // industry, but not this project's taxonomy, and mapping one onto the other
  // would file a company under a heading it was never assessed against.
  unclassified: "未分類",
};

export function subsectorLabel(value) {
  if (!value) return "";
  // An unknown slug shows itself rather than an empty cell: a universe entry
  // added without a label here is a gap to notice, not one to hide.
  return SUBSECTOR_LABELS[value] ?? value;
}
