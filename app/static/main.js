const usdFmt = new Intl.NumberFormat("en-US", {
  style: "currency",
  currency: "USD",
  maximumFractionDigits: 2,
});

const charts = {};
let quotesPollInFlight = false;

function destroyChart(key) {
  const existing = charts[key];
  if (existing) {
    existing.destroy();
    delete charts[key];
  }
}

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

function renderAllocationPie(chartKey, canvasId, emptyId, labels, values, percentages = []) {
  const canvas = document.getElementById(canvasId);
  const empty = document.getElementById(emptyId);
  if (!canvas || !window.Chart) return;

  const slices = labels
    .map((label, idx) => ({
      label,
      value: Number(values[idx] || 0),
      pct: Number(percentages[idx] || 0),
    }))
    .filter((slice) => slice.value > 0);

  if (slices.length === 0) {
    destroyChart(chartKey);
    canvas.classList.add("hidden");
    if (empty) empty.classList.remove("hidden");
    return;
  }

  if (empty) empty.classList.add("hidden");
  canvas.classList.remove("hidden");

  destroyChart(chartKey);
  charts[chartKey] = new Chart(canvas.getContext("2d"), {
    type: "pie",
    data: {
      labels: slices.map((slice) => `${slice.label} (${slice.pct.toFixed(1)}%)`),
      datasets: [
        {
          data: slices.map((slice) => slice.value),
          backgroundColor: ["#386fa4", "#f4a259", "#6b8e23", "#bc4749", "#7d4f50", "#2a9d8f", "#c44536", "#4a7c59"],
        },
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: {
          position: "top",
        },
        tooltip: {
          callbacks: {
            label(context) {
              return `${context.label}: ${formatUsd(context.raw)}`;
            },
          },
        },
      },
    },
  });
}

function renderLineChart(chartKey, canvasId, labels, values, datasetLabel, color) {
  const canvas = document.getElementById(canvasId);
  if (!canvas || !window.Chart) return;

  destroyChart(chartKey);
  charts[chartKey] = new Chart(canvas.getContext("2d"), {
    type: "line",
    data: {
      labels,
      datasets: [
        {
          label: datasetLabel,
          data: values,
          borderColor: color,
          pointRadius: 0,
          tension: 0.15,
        },
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
    },
  });
}

function renderPnlChart(canvasId, labels, values) {
  const canvas = document.getElementById(canvasId);
  if (!canvas || !window.Chart) return;

  destroyChart("pnl");
  charts.pnl = new Chart(canvas.getContext("2d"), {
    type: "line",
    data: {
      labels,
      datasets: [
        {
          label: "Unrealized PnL (USD)",
          data: values,
          pointRadius: 0,
          tension: 0.15,
          fill: {
            target: "origin",
            above: "rgba(17, 119, 68, 0.18)",
            below: "rgba(181, 51, 51, 0.18)",
          },
          segment: {
            borderColor(ctx) {
              const p0 = ctx.p0?.parsed?.y ?? 0;
              const p1 = ctx.p1?.parsed?.y ?? 0;
              if (p0 >= 0 && p1 >= 0) return "#117744";
              if (p0 < 0 && p1 < 0) return "#b53333";
              return "#4f616d";
            },
          },
          borderWidth: 2,
        },
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      scales: {
        y: {
          ticks: {
            callback(value) {
              return formatUsd(Number(value));
            },
          },
        },
      },
      plugins: {
        legend: {
          position: "top",
        },
        tooltip: {
          callbacks: {
            label(context) {
              return `${context.dataset.label}: ${formatUsd(Number(context.raw))}`;
            },
          },
        },
      },
    },
  });
}

function initAssetCreateForm() {
  const form = document.getElementById("assetCreateForm");
  const typeSelect = document.getElementById("assetTypeSelect");
  if (!form || !typeSelect) return;

  const marketFields = Array.from(form.querySelectorAll('[data-asset-create-field="market"]'));
  const manualFields = Array.from(form.querySelectorAll('[data-asset-create-field="manual"]'));
  const marketNote = form.querySelector('[data-asset-type-note="market"]');
  const manualNote = form.querySelector('[data-asset-type-note="manual"]');

  const qtyInput = form.querySelector('input[name="initial_quantity"]');
  const buyPriceInput = form.querySelector('input[name="initial_buy_price"]');
  const manualValueInput = form.querySelector('input[name="initial_value"]');

  const setVisibility = () => {
    const isMarket = typeSelect.value === "market";

    marketFields.forEach((el) => el.classList.toggle("hidden", !isMarket));
    manualFields.forEach((el) => el.classList.toggle("hidden", isMarket));

    if (marketNote) marketNote.classList.toggle("hidden", !isMarket);
    if (manualNote) manualNote.classList.toggle("hidden", isMarket);

    if (manualValueInput) manualValueInput.required = !isMarket;

    const qty = Number(qtyInput?.value || 0);
    if (buyPriceInput) buyPriceInput.required = isMarket && qty > 0;
  };

  if (qtyInput) {
    qtyInput.addEventListener("input", setVisibility);
  }
  typeSelect.addEventListener("change", setVisibility);
  setVisibility();
}

function initTxForm() {
  const form = document.getElementById("transactionForm");
  if (!form) return;

  const txType = document.getElementById("txType");
  if (!txType) return;

  const quantityField = form.querySelector('[data-field="quantity"]');
  const priceField = form.querySelector('[data-field="price"]');
  const manualField = form.querySelector('[data-field="manual_value"]');
  const manualInvestedOverrideField = form.querySelector('[data-field="manual_invested_override"]');
  const quantityInput = form.querySelector('input[name="quantity"]');
  const priceInput = form.querySelector('input[name="price"]');
  const manualValueInput = form.querySelector('input[name="manual_value"]');

  const update = () => {
    const isManualUpdate = txType.value === "MANUAL_VALUE_UPDATE";
    if (quantityField) quantityField.classList.toggle("hidden", isManualUpdate);
    if (priceField) priceField.classList.toggle("hidden", isManualUpdate);
    if (manualField) manualField.classList.toggle("hidden", !isManualUpdate);
    if (manualInvestedOverrideField) {
      manualInvestedOverrideField.classList.toggle("hidden", !isManualUpdate);
    }
    if (quantityInput) quantityInput.required = !isManualUpdate;
    if (priceInput) priceInput.required = !isManualUpdate;
    if (manualValueInput) manualValueInput.required = isManualUpdate;
  };

  txType.addEventListener("change", update);
  update();
}

function updateDashboardTable() {
  const rows = Array.from(document.querySelectorAll("#positionsTable tbody tr.asset-row"));
  const groupTotals = new Map();
  const groupChartTotals = new Map();
  const assetChartValues = new Map();
  const assetRowsById = new Map();

  let grandValue = 0;
  let grandPnl = 0;
  let derivedValue = 0;
  let derivedPnl = 0;

  rows.forEach((row) => {
    if ((row.dataset.rowKind || "asset") !== "basket") {
      const assetId = Number(row.dataset.assetId || 0);
      if (!Number.isNaN(assetId) && assetId > 0) {
        assetRowsById.set(assetId, row);
      }
    }
  });

  // Keep derived basket rows in sync when live quotes update member asset rows.
  rows.forEach((row) => {
    if ((row.dataset.rowKind || "asset") !== "basket") return;

    const memberIds = (row.dataset.basketMemberIds || "")
      .split(",")
      .map((value) => Number(value))
      .filter((value) => Number.isFinite(value) && value > 0);

    let basketValue = 0;
    let basketPnl = 0;
    let hasBasketPnl = false;
    let latestAsOfIso = "";

    memberIds.forEach((memberId) => {
      const memberRow = assetRowsById.get(memberId);
      if (!memberRow) return;

      basketValue += Number(memberRow.dataset.currentValue || 0);

      const memberPnlRaw = memberRow.dataset.currentPnl;
      if (memberPnlRaw !== "" && memberPnlRaw !== undefined) {
        const memberPnl = Number(memberPnlRaw);
        if (!Number.isNaN(memberPnl)) {
          basketPnl += memberPnl;
          hasBasketPnl = true;
        }
      }

      const memberAsOfIso = memberRow.dataset.asOfIso || "";
      if (memberAsOfIso && (!latestAsOfIso || memberAsOfIso > latestAsOfIso)) {
        latestAsOfIso = memberAsOfIso;
      }
    });

    row.dataset.currentValue = String(basketValue);
    row.dataset.currentPnl = hasBasketPnl ? String(basketPnl) : "";
    row.dataset.asOfIso = latestAsOfIso;

    const asOfCell = row.querySelector(".as-of");
    if (asOfCell) {
      asOfCell.textContent = latestAsOfIso ? formatAsOf(latestAsOfIso) : "-";
    }
  });

  rows.forEach((row) => {
    const groupName = row.dataset.group || "Ungrouped";
    const value = Number(row.dataset.currentValue || 0);
    const pnlRaw = row.dataset.currentPnl;
    const pnl = pnlRaw === "" || pnlRaw === undefined ? null : Number(pnlRaw);
    const countsInTotals = row.dataset.countsInTotals === "1";
    const countsInAllocation = row.dataset.countsInAllocation === "1";
    const rowKind = row.dataset.rowKind || "asset";

    if (countsInTotals) {
      grandValue += value;
      if (pnl !== null && !Number.isNaN(pnl)) {
        grandPnl += pnl;
      }
    } else {
      derivedValue += value;
      if (pnl !== null && !Number.isNaN(pnl)) {
        derivedPnl += pnl;
      }
    }

    if (countsInTotals) {
      if (!groupTotals.has(groupName)) {
        groupTotals.set(groupName, { value: 0, pnl: 0 });
      }
      const totals = groupTotals.get(groupName);
      totals.value += value;
      totals.pnl += pnl !== null && !Number.isNaN(pnl) ? pnl : 0;
    }

    if (countsInAllocation) {
      if (!groupChartTotals.has(groupName)) {
        groupChartTotals.set(groupName, 0);
      }
      groupChartTotals.set(groupName, groupChartTotals.get(groupName) + value);
    }

    if (value > 0) {
      const chartLabel = row.dataset.chartLabel || row.dataset.symbol || "Unknown";
      if (rowKind === "basket") {
        assetChartValues.set(chartLabel, (assetChartValues.get(chartLabel) || 0) + value);
      } else if (row.dataset.inBasketMember !== "1") {
        assetChartValues.set(chartLabel, (assetChartValues.get(chartLabel) || 0) + value);
      }
    }

    const valueCell = row.querySelector(".current-value");
    const pnlCell = row.querySelector(".pnl");
    if (valueCell) valueCell.textContent = formatUsd(value);
    if (pnlCell) {
      if (pnl === null || Number.isNaN(pnl)) {
        pnlCell.textContent = "-";
        pnlCell.classList.remove("positive", "negative");
      } else {
        pnlCell.textContent = formatUsd(pnl);
        pnlCell.classList.toggle("positive", pnl >= 0);
        pnlCell.classList.toggle("negative", pnl < 0);
      }
    }
  });

  rows.forEach((row) => {
    const value = Number(row.dataset.currentValue || 0);
    const allocCell = row.querySelector(".allocation");
    if (!allocCell) return;

    const countsInAllocation = row.dataset.countsInAllocation === "1";
    if (!countsInAllocation) {
      allocCell.textContent = "-";
      return;
    }

    const allocation = grandValue > 0 ? (value / grandValue) * 100 : 0;
    allocCell.textContent = formatPct(allocation);
  });

  groupTotals.forEach((totals, groupName) => {
    const subtotalRow = document.querySelector(`tr.group-subtotal[data-group-subtotal="${CSS.escape(groupName)}"]`);
    if (!subtotalRow) return;

    const valueCell = subtotalRow.querySelector(".group-value");
    const pnlCell = subtotalRow.querySelector(".group-pnl");

    if (valueCell) valueCell.textContent = formatUsd(totals.value);
    if (pnlCell) {
      pnlCell.textContent = formatUsd(totals.pnl);
      pnlCell.classList.toggle("positive", totals.pnl >= 0);
      pnlCell.classList.toggle("negative", totals.pnl < 0);
    }
  });

  const grandValueCell = document.getElementById("grandTotalValue");
  const grandPnlCell = document.getElementById("grandTotalPnl");

  if (grandValueCell) grandValueCell.textContent = formatUsd(grandValue);
  if (grandPnlCell) {
    grandPnlCell.textContent = formatUsd(grandPnl);
    grandPnlCell.classList.toggle("positive", grandPnl >= 0);
    grandPnlCell.classList.toggle("negative", grandPnl < 0);
  }

  const derivedValueCell = document.getElementById("derivedTotalValue");
  const derivedPnlCell = document.getElementById("derivedTotalPnl");
  if (derivedValueCell) derivedValueCell.textContent = formatUsd(derivedValue);
  if (derivedPnlCell) {
    derivedPnlCell.textContent = formatUsd(derivedPnl);
    derivedPnlCell.classList.toggle("positive", derivedPnl >= 0);
    derivedPnlCell.classList.toggle("negative", derivedPnl < 0);
  }

  const groupChartEntries = Array.from(groupChartTotals.entries()).filter(([, value]) => value > 0);
  const groupChartTotal = groupChartEntries.reduce((sum, [, value]) => sum + value, 0);
  const groupChartLabels = [];
  const groupChartValues = [];
  const groupChartPercentages = [];
  groupChartEntries.forEach(([label, value]) => {
    groupChartLabels.push(label);
    groupChartValues.push(value);
    groupChartPercentages.push(groupChartTotal > 0 ? (value / groupChartTotal) * 100 : 0);
  });

  const assetChartEntries = Array.from(assetChartValues.entries()).filter(([, value]) => value > 0);
  const assetChartTotal = assetChartEntries.reduce((sum, [, value]) => sum + value, 0);
  const assetChartLabels = [];
  const assetChartSlices = [];
  const assetChartPercentages = [];
  assetChartEntries.forEach(([label, value]) => {
    assetChartLabels.push(label);
    assetChartSlices.push(value);
    assetChartPercentages.push(assetChartTotal > 0 ? (value / assetChartTotal) * 100 : 0);
  });

  renderAllocationPie(
    "allocationByGroup",
    "allocationByGroupChart",
    "allocationByGroupEmpty",
    groupChartLabels,
    groupChartValues,
    groupChartPercentages,
  );
  renderAllocationPie(
    "allocationByAsset",
    "allocationByAssetChart",
    "allocationByAssetEmpty",
    assetChartLabels,
    assetChartSlices,
    assetChartPercentages,
  );
}

async function pollQuotes() {
  const cfg = window.PORTFOLIO_DASHBOARD;
  if (!cfg || !Array.isArray(cfg.symbols) || cfg.symbols.length === 0) return;
  if (quotesPollInFlight) return;

  const url = `/api/quotes?symbols=${encodeURIComponent(cfg.symbols.join(","))}`;
  quotesPollInFlight = true;
  try {
    const response = await fetch(url, { credentials: "same-origin" });
    if (!response.ok) return;

    const body = await response.json();
    const quotes = body.quotes || {};

    Object.entries(quotes).forEach(([symbol, quote]) => {
      const rows = document.querySelectorAll(`tr.asset-row[data-symbol="${CSS.escape(symbol)}"]`);
      rows.forEach((row) => {
        if (row.dataset.assetType !== "market") return;

        const quantity = Number(row.dataset.quantity || 0);
        const avgCost = Number(row.dataset.avgCost || 0);

        const priceCell = row.querySelector(".current-price");
        const asOfCell = row.querySelector(".as-of");

        if (quote.price === null || quote.price === undefined) {
          row.dataset.currentValue = "0";
          row.dataset.currentPnl = quantity > 0 ? String(-avgCost * quantity) : "";
          row.dataset.asOfIso = quote.as_of || "";
          if (priceCell) priceCell.textContent = "-";
          if (asOfCell) asOfCell.textContent = quote.as_of ? formatAsOf(quote.as_of) : "-";
          return;
        }

        const price = Number(quote.price);
        const value = quantity * price;
        const pnl = (price - avgCost) * quantity;

        row.dataset.currentValue = String(value);
        row.dataset.currentPnl = String(pnl);
        row.dataset.asOfIso = quote.as_of || "";

        if (priceCell) {
          priceCell.textContent = formatUsd(price);
          priceCell.classList.toggle("stale", Boolean(quote.stale));
        }
        if (asOfCell) asOfCell.textContent = formatAsOf(quote.as_of);
      });
    });

    updateDashboardTable();
  } catch (_error) {
    // Best effort refresh only.
  } finally {
    quotesPollInFlight = false;
  }
}

function initDashboard() {
  const cfg = window.PORTFOLIO_DASHBOARD;
  if (!cfg) return;

  updateDashboardTable();
  initPortfolioChart();
  initPnlChartWithSelectors();
  pollQuotes();
  window.setInterval(pollQuotes, 60000);
}

function initAssetChart() {
  const cfg = window.ASSET_CHART;
  if (!cfg || !Array.isArray(cfg.labels) || cfg.labels.length === 0) return;
  renderLineChart("asset", "assetChart", cfg.labels, cfg.values, "Close", "#285d8f");
}

function initBasketChart() {
  const cfg = window.BASKET_CHART;
  if (!cfg || !Array.isArray(cfg.labels) || cfg.labels.length === 0) return;
  renderLineChart("basket", "basketChart", cfg.labels, cfg.values, "Basket (Base=100)", "#bc4749");
}

function initPortfolioChart() {
  const cfg = window.PORTFOLIO_DASHBOARD;
  if (!cfg) return;

  const labels = Array.isArray(cfg.portfolioSeriesLabels) ? cfg.portfolioSeriesLabels : [];
  const values = Array.isArray(cfg.portfolioSeriesValues) ? cfg.portfolioSeriesValues : [];
  const canvas = document.getElementById("portfolioChart");
  if (!canvas) return;

  if (labels.length === 0 || values.length === 0) {
    destroyChart("portfolio");
    canvas.classList.add("hidden");
    return;
  }

  canvas.classList.remove("hidden");
  renderLineChart("portfolio", "portfolioChart", labels, values, "Portfolio Value (USD)", "#2a9d8f");
}

function initPnlChartWithSelectors() {
  const cfg = window.PORTFOLIO_DASHBOARD;
  if (!cfg) return;

  const labels = Array.isArray(cfg.pnlSeriesLabels) ? cfg.pnlSeriesLabels : [];
  const seriesByAsset = cfg.pnlSeriesByAsset && typeof cfg.pnlSeriesByAsset === "object"
    ? cfg.pnlSeriesByAsset
    : {};
  const selectors = Array.from(document.querySelectorAll(".pnl-asset-selector"));
  const canvas = document.getElementById("pnlChart");
  const emptyMsg = document.getElementById("pnlSelectionEmpty");
  if (!canvas) return;

  const redraw = () => {
    const selected = selectors
      .filter((input) => input.checked)
      .map((input) => input.value)
      .filter((label) => Object.prototype.hasOwnProperty.call(seriesByAsset, label));

    if (selected.length === 0 || labels.length === 0) {
      destroyChart("pnl");
      canvas.classList.add("hidden");
      if (emptyMsg) emptyMsg.classList.remove("hidden");
      return;
    }

    const combined = labels.map(() => 0);
    selected.forEach((label) => {
      const values = Array.isArray(seriesByAsset[label]) ? seriesByAsset[label] : [];
      values.forEach((value, idx) => {
        combined[idx] += Number(value || 0);
      });
    });

    if (emptyMsg) emptyMsg.classList.add("hidden");
    canvas.classList.remove("hidden");
    renderPnlChart("pnlChart", labels, combined);
  };

  selectors.forEach((input) => {
    input.addEventListener("change", redraw);
  });
  redraw();
}

document.addEventListener("DOMContentLoaded", () => {
  initAssetCreateForm();
  initTxForm();
  initDashboard();
  initAssetChart();
  initBasketChart();
});
