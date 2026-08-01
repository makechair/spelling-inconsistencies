const status = document.getElementById('coverage-status');
const content = document.getElementById('coverage-content');
const count = document.getElementById('coverage-count');

const formatDate = (value) => value.replaceAll('-', '/');

async function loadCoverage() {
  try {
    const response = await fetch('/api/coverage', { credentials: 'same-origin' });
    if (response.status === 401 || response.status === 403) {
      window.location.reload();
      return;
    }
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const rows = await response.json();
    count.textContent = rows.length.toLocaleString('ja-JP');
    content.replaceChildren(...rows.map((row) => {
      const item = document.createElement('article');
      item.className = 'coverage-row';
      const link = document.createElement('a');
      link.className = 'coverage-symbol';
      link.href = `/?symbol=${encodeURIComponent(row.symbol)}`;
      link.textContent = row.symbol;
      const range = document.createElement('span');
      range.className = 'coverage-range';
      range.textContent = `${formatDate(row.first_date)}～${formatDate(row.last_date)}`;
      item.append(link, range);
      return item;
    }));
    status.hidden = rows.length > 0;
    if (!rows.length) status.textContent = '取得済みの株価データはまだありません。';
    content.hidden = rows.length === 0;
  } catch (error) {
    status.textContent = `蓄積状況の読み込みに失敗しました: ${error.message}`;
  }
}

loadCoverage();
