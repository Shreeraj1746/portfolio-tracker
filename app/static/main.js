const usdFmt = new Intl.NumberFormat("en-US", {
  style: "currency",
  currency: "USD",
  maximumFractionDigits: 2,
});

function formatUsd(value) {
  if (value === null || value === undefined || Number.isNaN(value)) return "-";
  return usdFmt.format(value);
}

function formatPct(value) {
  if (value === null || value === undefined || Number.isNaN(value)) return "-";
  return `${value.toFixed(2)}%`;
}

function formatAsOf(value) {
  if (!value) return "-";
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return value;
  return `${d.toISOString().slice(0, 19).replace("T", " ")} UTC`;
}

function initTxForm() {
  const form = document.getElementById("transactionForm");
  if (!form) return;

  const txType = document.getElementById("txType");
  if (!txType) return;

  const quantityField = form.querySelector('[data-field="quantity"]');
  const priceField = form.querySelector('[data-field="price"]');
  const manualField = form.querySelector('[data-field="manual_value"]');

  const update = () => {
    const value = txType.value;
    const isManualUpdate = value === "MANUAL_VALUE_UPDATE";
    if (quantityField) quantityField.style.display = isManualUpdate ? "none" : "grid";
    if (priceField) priceField.style.display = isManualUpdate ? "none" : "grid";
    if (manualField) manualField.style.display = isManualUpdate ? "grid" : "none";
  };

  txType.addEventListener("change", update);
  update();
}

let allocationChart = null;

function initAllocationChart() {
  const cfg = window.PORTFOLIO_DASHBOARD;
  const canvas = document.getElementById("allocationChart");
  if (!cfg || !canvas || !window.Chart) return;

  allocationChart = new Chart(canvas.getContext("2d"), {
    type: "pie",
    data: {
      labels: cfg.allocationLabels,
      datasets: [
        {
          data: cfg.allocationValues,
          backgroundColor: ["#386fa4", "#f4a259", "#6b8e23", "#bc4749", "#7d4f50", "#2a9d8f"],
        },
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
    },
  });
}

function refreshDashboardTotals() {
  const rows = Array.from(document.querySelectorAll("#positionsTable tbody tr.asset-row"));
  const groupTotals = new Map();
  let grandValue = 0;
  let grandPnl = 0;

  rows.forEach((row) => {
    const valueCell = row.querySelector(".current-value");
    const pnlCell = row.querySelector(".pnl");
    const groupName = row.dataset.group || "Ungrouped";

    const value = Number(row.dataset.currentValue || 0);
    const pnl = Number(row.dataset.currentPnl || 0);

    grandValue += value;
    grandPnl += pnl;

    if (!groupTotals.has(groupName)) {
      groupTotals.set(groupName, { value: 0, pnl: 0 });
    }
    const totals = groupTotals.get(groupName);
    totals.value += value;
    totals.pnl += pnl;

    if (valueCell) valueCell.textContent = formatUsd(value);
    if (pnlCell) {
      pnlCell.textContent = row.dataset.currentPnl ? formatUsd(pnl) : "-";
      pnlCell.classList.toggle("positive", pnl >= 0);
      pnlCell.classList.toggle("negative", pnl < 0);
    }
  });

  rows.forEach((row) => {
    const value = Number(row.dataset.currentValue || 0);
    const allocation = grandValue > 0 ? (value / grandValue) * 100 : 0;
    const allocCell = row.querySelector(".allocation");
    if (allocCell) allocCell.textContent = formatPct(allocation);
  });

  groupTotals.forEach((totals, groupName) => {
    const row = document.querySelector(`tr.group-subtotal[data-group-subtotal="${CSS.escape(groupName)}"]`);
    if (!row) return;
    const valueCell = row.querySelector(".group-value");
    const pnlCell = row.querySelector(".group-pnl");
    if (valueCell) valueCell.textContent = formatUsd(totals.value);
    if (pnlCell) {
      pnlCell.textContent = formatUsd(totals.pnl);
      pnlCell.classList.toggle("positive", totals.pnl >= 0);
      pnlCell.classList.toggle("negative", totals.pnl < 0);
    }
  });

  const grandTotalValue = document.getElementById("grandTotalValue");
  const grandTotalPnl = document.getElementById("grandTotalPnl");
  if (grandTotalValue) grandTotalValue.textContent = formatUsd(grandValue);
  if (grandTotalPnl) {
    grandTotalPnl.textContent = formatUsd(grandPnl);
    grandTotalPnl.classList.toggle("positive", grandPnl >= 0);
    grandTotalPnl.classList.toggle("negative", grandPnl < 0);
  }

  if (allocationChart) {
    allocationChart.data.labels = Array.from(groupTotals.keys());
    allocationChart.data.datasets[0].data = Array.from(groupTotals.values()).map((v) => v.value);
    allocationChart.update();
  }
}

async function pollQuotes() {
  const cfg = window.PORTFOLIO_DASHBOARD;
  if (!cfg || !cfg.symbols || cfg.symbols.length === 0) return;

  const url = `/api/quotes?symbols=${encodeURIComponent(cfg.symbols.join(","))}`;
  try {
    const res = await fetch(url, { credentials: "same-origin" });
    if (!res.ok) return;
    const body = await res.json();
    const quotes = body.quotes || {};

    Object.entries(quotes).forEach(([symbol, data]) => {
      const rows = document.querySelectorAll(`tr.asset-row[data-symbol="${CSS.escape(symbol)}"]`);
      rows.forEach((row) => {
        if (row.dataset.assetType !== "market") return;

        const quantity = Number(row.dataset.quantity || 0);
        const avgCost = Number(row.dataset.avgCost || 0);

        if (data.price !== null && data.price !== undefined) {
          const price = Number(data.price);
          const value = quantity * price;
          const pnl = (price - avgCost) * quantity;

          row.dataset.currentValue = String(value);
          row.dataset.currentPnl = String(pnl);

          const priceCell = row.querySelector(".current-price");
          const asOfCell = row.querySelector(".as-of");
          if (priceCell) {
            priceCell.textContent = formatUsd(price);
            priceCell.classList.toggle("stale", Boolean(data.stale));
          }
          if (asOfCell) asOfCell.textContent = formatAsOf(data.as_of);
        }
      });
    });

    refreshDashboardTotals();
  } catch (_err) {
    // Best effort polling only.
  }
}

function initDashboardPolling() {
  if (!window.PORTFOLIO_DASHBOARD) return;

  const rows = Array.from(document.querySelectorAll("#positionsTable tbody tr.asset-row"));
  rows.forEach((row) => {
    const valueCell = row.querySelector(".current-value");
    const pnlCell = row.querySelector(".pnl");
    if (valueCell) {
      const value = Number((valueCell.textContent || "").replace(/[^0-9.-]/g, ""));
      row.dataset.currentValue = Number.isNaN(value) ? "0" : String(value);
    }
    if (pnlCell) {
      const pnl = Number((pnlCell.textContent || "").replace(/[^0-9.-]/g, ""));
      row.dataset.currentPnl = Number.isNaN(pnl) ? "" : String(pnl);
    }
  });

  pollQuotes();
  window.setInterval(pollQuotes, 60000);
}

function initAssetChart() {
  const cfg = window.ASSET_CHART;
  const canvas = document.getElementById("assetChart");
  if (!cfg || !canvas || !window.Chart) return;

  new Chart(canvas.getContext("2d"), {
    type: "line",
    data: {
      labels: cfg.labels,
      datasets: [
        {
          label: "Close",
          data: cfg.values,
          borderColor: "#285d8f",
          pointRadius: 0,
          tension: 0.18,
        },
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      scales: {
        y: {
          ticks: {
            callback: (val) => usdFmt.format(val),
          },
        },
      },
    },
  });
}

function initBasketChart() {
  const cfg = window.BASKET_CHART;
  const canvas = document.getElementById("basketChart");
  if (!cfg || !canvas || !window.Chart) return;

  new Chart(canvas.getContext("2d"), {
    type: "line",
    data: {
      labels: cfg.labels,
      datasets: [
        {
          label: "Basket (Base=100)",
          data: cfg.values,
          borderColor: "#bc4749",
          pointRadius: 0,
          tension: 0.12,
        },
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
    },
  });
}

document.addEventListener("DOMContentLoaded", () => {
  initTxForm();
  initAllocationChart();
  initDashboardPolling();
  initAssetChart();
  initBasketChart();
});
