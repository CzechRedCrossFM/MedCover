/* Generic client-side sortable table.
 * Requires table rows to carry data-sv='{"col": value, ...}' attributes.
 * Sortable headers must have class "sortable" and data-col="<key>".
 * Set data-sort-initial="none" to preserve the server's initial order.
 * Grouped tables use one tbody per group and data-sort-group="<key>" on the table;
 * each tbody carries its group value in data-sv. Subtotals use data-sort-fixed. */
(function () {
  document.querySelectorAll('table[id] thead').forEach(function (thead) {
    var table  = thead.closest('table');
    var bodies = Array.from(table.tBodies);
    if (!bodies.length) return;

    var sortCol = null;
    var sortDir = 1;

    function val(row, col) {
      try {
        var sv = row.dataset.sv ? JSON.parse(row.dataset.sv) : {};
        return sv[col] !== undefined ? sv[col] : null;
      } catch (e) { return null; }
    }

    function compare(a, b) {
      var av = val(a, sortCol), bv = val(b, sortCol);
      // Missing dates or hours stay last in either direction; zero is a value.
      if (av === null || av === '') return (bv === null || bv === '') ? 0 : 1;
      if (bv === null || bv === '') return -1;
      if (typeof av === 'number' && typeof bv === 'number') return (av - bv) * sortDir;
      return String(av).localeCompare(String(bv), 'cs') * sortDir;
    }

    function sort() {
      if (!sortCol) return;
      if (table.dataset.sortGroup === sortCol) {
        bodies.sort(compare).forEach(function (body) { table.insertBefore(body, table.tFoot); });
      } else {
        bodies.forEach(function (body) {
          var rows = Array.from(body.rows).filter(function (row) { return row.hasAttribute('data-sv'); });
          var subtotal = body.querySelector('[data-sort-fixed]');
          rows.sort(compare).forEach(function (row) { body.insertBefore(row, subtotal); });
        });
      }
      thead.querySelectorAll('th.sortable').forEach(function (th) {
        var icon = th.querySelector('.sort-icon');
        if (icon) icon.textContent = th.dataset.col === sortCol ? (sortDir === 1 ? '↑' : '↓') : '↕';
        th.setAttribute('aria-sort', th.dataset.col === sortCol ? (sortDir === 1 ? 'ascending' : 'descending') : 'none');
      });
    }

    thead.querySelectorAll('th.sortable').forEach(function (th) {
      th.addEventListener('click', function () {
        if (sortCol === th.dataset.col) {
          sortDir *= -1;
        } else {
          sortCol = th.dataset.col;
          sortDir = 1;
        }
        sort();
      });
    });

    // Auto-sort by first sortable column on load
    var firstSortable = thead.querySelector('th.sortable');
    if (firstSortable && table.dataset.sortInitial !== 'none') {
      sortCol = firstSortable.dataset.col;
      sort();
    }
  });
})();
