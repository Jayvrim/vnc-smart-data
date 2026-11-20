const $ = (id) => document.getElementById(id);
const state = { token: localStorage.getItem("smartdata_token"), user: null, plans: [], selectedPlan: null, connectionActive: localStorage.getItem("vnc_connection_active") === "true", connecting: false, simulatedRoute: localStorage.getItem("vnc_simulated_route") || (localStorage.getItem("vnc_connection_active") === "true" ? "B" : null), selectedNetwork: null, availableBalanceGb: 0, networkDemoTimer: null, networkDemoBusy: false, usageTimer: null, usageReportBusy: false, networkDemoIndex: 0 };

const AUTO_USAGE_GB_PER_TICK = 0.005;
const AUTO_USAGE_INTERVAL_MS = 30_000;
const NETWORK_DEMO = [
  { networks: [{ type: "5G", route: "A", speedMbps: 720, latency: 18, signal: 94, status: "Excellent" }, { type: "4G", route: "B", speedMbps: 165, latency: 31, signal: 89, status: "Strong" }] },
  { networks: [{ type: "5G", route: "A", speedMbps: 440, latency: 23, signal: 78, status: "Strong" }, { type: "4G", route: "B", speedMbps: 185, latency: 29, signal: 90, status: "Excellent" }] },
  { networks: [{ type: "5G", route: "A", speedMbps: 130, latency: 49, signal: 52, status: "Fair" }, { type: "4G", route: "B", speedMbps: 205, latency: 27, signal: 91, status: "Excellent" }] },
  { networks: [{ type: "5G", route: "A", speedMbps: 35, latency: 142, signal: 24, status: "Weak" }, { type: "4G", route: "B", speedMbps: 140, latency: 34, signal: 84, status: "Strong" }] },
  { networks: [{ type: "5G", route: "A", speedMbps: 510, latency: 20, signal: 81, status: "Strong" }, { type: "4G", route: "B", speedMbps: 112, latency: 39, signal: 68, status: "Fair" }] },
  { networks: [{ type: "5G", route: "A", speedMbps: 88, latency: 76, signal: 44, status: "Fair" }, { type: "4G", route: "B", speedMbps: 220, latency: 25, signal: 95, status: "Excellent" }] }
];

function fastestNetwork(networks) {
  return [...networks].sort((a, b) =>
    b.speedMbps - a.speedMbps || a.latency - b.latency || b.signal - a.signal
  )[0];
}

async function api(path, options = {}) {
  const headers = { "Content-Type": "application/json", ...(options.headers || {}) };
  if (state.token) headers.Authorization = `Bearer ${state.token}`;
  const response = await fetch(path, { ...options, headers });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || "Request failed");
  return data;
}

function showToast(message) {
  $("toast").textContent = message;
  $("toast").classList.add("show");
  setTimeout(() => $("toast").classList.remove("show"), 2600);
}

function showPage(name) {
  document.querySelectorAll(".page").forEach((page) => page.classList.toggle("active", page.id === `${name}Page`));
  document.querySelectorAll(".nav-item").forEach((item) => item.classList.toggle("active", item.dataset.page === name));
  document.querySelector(".main-content").scrollTo({ top: 0, behavior: "smooth" });
}

function money(kobo) {
  return new Intl.NumberFormat("en-NG", { style: "currency", currency: "NGN", maximumFractionDigits: 0 }).format(kobo / 100);
}

async function loadDashboard() {
  try {
    const dashboard = await api("/api/dashboard");
    state.availableBalanceGb = dashboard.balance_gb;
    $("balanceValue").textContent = dashboard.balance_gb.toFixed(3);
    $("cashWalletValue").dataset.balanceKobo = String(dashboard.cash_wallet_balance_kobo);
    $("cashWalletValue").textContent = money(dashboard.cash_wallet_balance_kobo);
    $("checkoutWalletBalance").textContent = money(dashboard.cash_wallet_balance_kobo);
    $("homeUsageValue").textContent = `${dashboard.usage_gb.toFixed(3)} GB`;
    $("usageBalance").textContent = `${dashboard.balance_gb.toFixed(3)} GB`;
    $("usageTotal").textContent = dashboard.usage_gb.toFixed(3);
    const totalData = dashboard.balance_gb + dashboard.usage_gb;
    const pct = totalData > 0 ? Math.min(100, dashboard.usage_gb / totalData * 100) : 0;
    $("usageTrackBar").style.width = `${pct}%`;
    $("usageTrack").setAttribute("aria-valuenow", pct.toFixed(1));
    $("usageCaption").textContent = dashboard.demo_usage_gb > 0
      ? `${dashboard.demo_usage_gb.toFixed(3)} GB simulated for testing. Actual device traffic is not being measured.`
      : "Usage is not being measured on this device. Connect to start automatic demo usage.";
    $("activePlanName").textContent = dashboard.subscription ? `${dashboard.subscription.package_key} package` : "No active plan";
    $("noticeCount").textContent = dashboard.notifications.filter((item) => !item.read).length;
    $("paymentActivity").textContent = dashboard.payments.length
      ? dashboard.payments.map((payment) => {
        const demo = payment.mode === "SIMULATED_DEMO";
        const label = demo
          ? "Demo purchase · no payment"
          : payment.mode === "VNC_WALLET"
            ? "VNC wallet · paid"
            : `Interswitch · ${payment.status}`;
        return `${label} · ${payment.package_name} · ${money(payment.amount_kobo)}`;
      }).join("\n")
      : "No transactions yet.";
  } catch (error) {
    showToast(error.message);
  }
}

async function loadPlans() {
  state.plans = await api("/api/plans");
  $("plansList").innerHTML = state.plans.map((plan) => {
    const dataLabel = plan.gb < 1 ? `${Math.round(plan.gb * 1000)} MB` : `${plan.gb} GB`;
    return `
    <article class="plan-card">
      <div><h3>${plan.name}</h3><p>${dataLabel} · ${plan.days} day${plan.days === 1 ? "" : "s"}</p></div>
      <div><div class="price">${money(plan.price_kobo)}<small>one-time</small></div>
        <button class="choose-plan" data-plan="${plan.key}">Choose plan</button>
      </div>
    </article>`;
  }).join("");
  document.querySelectorAll(".choose-plan").forEach((button) => button.addEventListener("click", () => openCheckout(button.dataset.plan)));
}

async function openCheckout(packageKey) {
  state.selectedPlan = state.plans.find((plan) => plan.key === packageKey);
  if (!state.selectedPlan) return;
  $("checkoutPlanName").textContent = `${state.selectedPlan.name} · ${state.selectedPlan.gb} GB`;
  $("checkoutAmount").textContent = money(state.selectedPlan.price_kobo);
  $("checkoutWalletBalance").textContent = money(Number($("cashWalletValue").dataset.balanceKobo || 0));
  $("walletPayBtn").disabled = false;
  $("checkoutStatus").className = "inline-status";
  $("checkoutStatus").textContent = "";
  $("checkoutModal").classList.add("open");
}

async function purchaseWithWallet() {
  if (!state.selectedPlan) return;
  const button = $("walletPayBtn");
  button.disabled = true;
  let purchased = false;
  const storageKey = `smartdata_wallet_purchase:${state.selectedPlan.key}`;
  let idempotencyKey = localStorage.getItem(storageKey);
  if (!idempotencyKey) {
    idempotencyKey = window.crypto && typeof window.crypto.randomUUID === "function"
      ? window.crypto.randomUUID()
      : `${Date.now()}-${Math.random().toString(36).slice(2)}`;
    localStorage.setItem(storageKey, idempotencyKey);
  }
  $("checkoutStatus").className = "inline-status show";
  $("checkoutStatus").textContent = "Purchasing data from your VNC wallet…";
  try {
    const result = await api("/api/wallet/purchase", {
      method: "POST",
      body: JSON.stringify({
        package: state.selectedPlan.key,
        idempotency_key: idempotencyKey
      })
    });
    localStorage.removeItem(storageKey);
    await loadDashboard();
    purchased = true;
    $("checkoutStatus").textContent =
      `${result.gb_added} GB added. ${money(result.amount_kobo)} deducted from your VNC wallet.`;
  } catch (error) {
    $("checkoutStatus").className = "inline-status show error";
    $("checkoutStatus").textContent = error.message;
  } finally {
    button.disabled = purchased;
  }
}

async function initiateInterswitch() {
  if (!state.selectedPlan) return;
  $("checkoutStatus").className = "inline-status show";
  $("checkoutStatus").textContent = "Creating a verified payment reference…";
  let intent;
  try {
    intent = await api("/api/payments/initiate", { method: "POST", body: JSON.stringify({ package: state.selectedPlan.key }) });
  } catch (error) {
    $("checkoutStatus").className = "inline-status show error";
    $("checkoutStatus").textContent = error.message;
    return;
  }
  sessionStorage.setItem("smartdata_checkout_intent", JSON.stringify({
    package: state.selectedPlan.key,
    packageName: state.selectedPlan.name,
    amountKobo: intent.amount_kobo,
    serverTxnRef: intent.txn_ref
  }));
  const nairaAmount = (intent.amount_kobo / 100).toFixed(2);
  const checkoutUrl = `/checkout.html?amount=${encodeURIComponent(nairaAmount)}&package=${encodeURIComponent(state.selectedPlan.key)}&txn=${encodeURIComponent(intent.txn_ref)}&merchant_code=${encodeURIComponent(intent.merchant_code)}&pay_item_id=${encodeURIComponent(intent.pay_item_id)}&mode=${encodeURIComponent(intent.mode)}`;
  const checkoutWindow = window.open(checkoutUrl, "smartdata-interswitch");
  if (!checkoutWindow) {
    $("checkoutStatus").className = "inline-status show error";
    $("checkoutStatus").textContent = "The browser blocked the checkout window. Allow pop-ups and try again.";
    return;
  }
  $("checkoutStatus").textContent = `Opening Interswitch for ${state.selectedPlan.name} · ₦${nairaAmount}.`;
}

async function addWalletFunds() {
  const input = window.prompt("Amount to add in naira (minimum ₦10)", "10");
  if (input === null) return;
  const amount = Number(input);
  if (!Number.isFinite(amount) || amount < 10) {
    showToast("Enter a valid amount of at least ₦10.");
    return;
  }

  try {
    const intent = await api("/api/wallet/topups/initiate", {
      method: "POST",
      body: JSON.stringify({ amount_naira: amount })
    });
    const checkoutUrl = `/checkout.html?amount=${encodeURIComponent((intent.amount_kobo / 100).toFixed(2))}&txn=${encodeURIComponent(intent.txn_ref)}&merchant_code=${encodeURIComponent(intent.merchant_code)}&pay_item_id=${encodeURIComponent(intent.pay_item_id)}&mode=${encodeURIComponent(intent.mode)}`;
    const checkoutWindow = window.open(checkoutUrl, "smartdata-interswitch");
    if (!checkoutWindow) {
      showToast("The browser blocked the checkout window. Allow pop-ups and try again.");
    }
  } catch (error) {
    showToast(error.message);
  }
}

async function addDemoCredit() {
  if (!state.selectedPlan) return;
  $("checkoutStatus").className = "inline-status show";
  $("checkoutStatus").textContent = "Adding clearly labelled demo data…";
  try {
    const result = await api("/api/payments/demo-credit", {
      method: "POST",
      body: JSON.stringify({ package: state.selectedPlan.key })
    });
    $("checkoutStatus").textContent = `DEMO ONLY · ${result.gb_added} GB added. No payment was taken.`;
    await loadDashboard();
  } catch (error) {
    $("checkoutStatus").className = "inline-status show error";
    $("checkoutStatus").textContent = error.message;
  }
}

function restoreSimulatedRoute() {
  if (!state.simulatedRoute) return;
  $("currentRoute").textContent = `Smart Route ${state.simulatedRoute}`;
  $("selectedRouteName").textContent = `Smart Route ${state.simulatedRoute}`;
  $("qualityRoute").textContent = `Smart Route ${state.simulatedRoute}`;
  $("routeExplanation").textContent = "Connect to compare simulated 5G and 4G speeds and auto-select the fastest.";
}

async function selectRoute(key) {
  const result = await api("/api/routes/select", { method: "POST", body: JSON.stringify({ route: key }) });
  state.simulatedRoute = key;
  localStorage.setItem("vnc_simulated_route", key);
  $("selectedRouteName").textContent = result.route;
  $("currentRoute").textContent = result.route;
  $("routeExplanation").textContent = result.explanation;
  $("routeScore").textContent = "—";
  $("connectionTitle").textContent = state.connectionActive ? `VNC session · ${result.route}` : `${result.route} selected`;
  $("connectionDescription").textContent = "Simulated route selection only. No carrier network was changed.";
  return result;
}

function renderNetworkCandidates(scenario, selected) {
  for (const type of ["5G", "4G"]) {
    const network = scenario.networks.find((candidate) => candidate.type === type);
    const isFastest = selected?.type === type;
    const card = $(`network${type}Card`);
    card.classList.toggle("fastest", Boolean(isFastest && state.connectionActive));
    $(`network${type}Speed`).textContent = `${network.speedMbps} Mbps`;
    $(`network${type}Latency`).textContent = `${network.latency} ms`;
    $(`network${type}Signal`).textContent = `${network.signal}%`;
    $(`network${type}Badge`).textContent = isFastest
      ? state.connectionActive ? "FASTEST" : "LAST FASTEST"
      : "AVAILABLE";
  }
}

function renderNetworkQuality(sample, scenario, explanation) {
  $("qualityValue").textContent = sample.signal;
  $("qualityStatus").textContent = `${sample.status} · ${sample.type}`;
  $("qualityRoute").textContent = `${sample.type} · Smart Route ${sample.route}`;
  $("qualityLatency").textContent = `${sample.speedMbps} Mbps · ${sample.latency} ms · demo`;
  $("qualityExplanation").textContent = explanation;
  $("routeScore").textContent = sample.speedMbps;
  $("selectedRouteName").textContent = `${sample.type} · fastest`;
  $("currentRoute").textContent = `Smart Route ${sample.route}`;
  $("networkType").textContent = `${sample.type} · fastest (demo)`;
  $("measurementValue").textContent = `${sample.speedMbps} Mbps · demo`;
  $("measurementNote").textContent = "Simulated throughput; not measured from this device";
  $("connectionTitle").textContent = `VNC session · ${sample.type} fastest (demo)`;
  $("connectionDescription").textContent = "Automatic choice is simulated; no physical carrier network changed.";
  renderNetworkCandidates(scenario, sample);
  const meter = $("qualityMeterFill");
  meter.style.width = `${sample.signal}%`;
  meter.classList.toggle("weak", sample.signal < 30);
  $("qualityCard").querySelector(".quality-meter").setAttribute("aria-valuenow", String(sample.signal));
  const activeBars = Math.ceil(sample.signal / 20);
  $("qualityBars").querySelectorAll("i").forEach((bar, index) => {
    bar.classList.toggle("active", index < activeBars);
  });
}

async function advanceNetworkDemo() {
  if (state.networkDemoBusy || !state.networkDemoTimer) return;
  state.networkDemoBusy = true;
  const scenario = NETWORK_DEMO[state.networkDemoIndex];
  const selected = fastestNetwork(scenario.networks);
  const other = scenario.networks.find((network) => network.type !== selected.type);
  const previous = state.selectedNetwork;
  try {
    if (selected.route !== state.simulatedRoute) {
      await selectRoute(selected.route);
    }
    state.selectedNetwork = selected;
    const switched = previous && previous.type !== selected.type
      ? ` Switched from ${previous.type} to ${selected.type}.`
      : "";
    const explanation = `Auto-selected ${selected.type}: ${selected.speedMbps} Mbps simulated versus ${other.speedMbps} Mbps on ${other.type}.${switched}`;
    $("routeExplanation").textContent = explanation;
    renderNetworkQuality(selected, scenario, explanation);
    state.networkDemoIndex = (state.networkDemoIndex + 1) % NETWORK_DEMO.length;
  } catch (error) {
    pauseNetworkAutomation(error);
  } finally {
    state.networkDemoBusy = false;
  }
}

function stopNetworkDemo() {
  if (state.networkDemoTimer) clearInterval(state.networkDemoTimer);
  state.networkDemoTimer = null;
  state.networkDemoBusy = false;
  state.selectedNetwork = null;
}

function stopConnectionAutomation() {
  if (state.usageTimer) clearInterval(state.usageTimer);
  state.usageTimer = null;
  stopNetworkDemo();
  const lastScenario = NETWORK_DEMO[(state.networkDemoIndex + NETWORK_DEMO.length - 1) % NETWORK_DEMO.length];
  renderNetworkCandidates(lastScenario, null);
  $("qualityAutoStatus").textContent = "Automatic signal and route changes pause while disconnected.";
  $("automaticUsageStatus").textContent = "Demo usage pauses while disconnected or when the app is closed. Actual phone traffic is not measured.";
}

function pauseNetworkAutomation(error) {
  if (state.networkDemoTimer) clearInterval(state.networkDemoTimer);
  state.networkDemoTimer = null;
  $("qualityAutoStatus").textContent = "Automatic route selection paused. Your VNC session remains connected.";
  $("connectionDescription").textContent =
    `Route simulation paused: ${error.message}. Your VNC session remains connected.`;
  showToast("VNC remains connected; automatic route simulation is paused.");
}

function pauseAutomaticUsage(message) {
  if (state.usageTimer) clearInterval(state.usageTimer);
  state.usageTimer = null;
  $("automaticUsageStatus").textContent = message;
}

async function reportAutomaticUsage() {
  if (!state.connectionActive || !navigator.onLine || state.usageReportBusy) return;
  const gbUsed = Number(Math.min(AUTO_USAGE_GB_PER_TICK, state.availableBalanceGb).toFixed(3));
  if (gbUsed <= 0) {
    pauseAutomaticUsage("No data balance remains. Demo usage is paused; your VNC session remains connected.");
    return;
  }
  state.usageReportBusy = true;
  try {
    const result = await api("/api/usage/report", {
      method: "POST",
      body: JSON.stringify({ gb_used: gbUsed, source: "demo_simulation" })
    });
    state.availableBalanceGb = result.available_balance_gb;
    await loadDashboard();
    if (state.availableBalanceGb <= 0) {
      pauseAutomaticUsage("No data balance remains. Demo usage is paused; your VNC session remains connected.");
      showToast("Your data balance is empty. VNC remains connected; demo usage is paused.");
    }
  } catch (error) {
    const balanceEmpty = error.message.includes("Insufficient available balance");
    if (balanceEmpty) await loadDashboard();
    pauseAutomaticUsage(
      balanceEmpty
        ? "No data balance remains. Demo usage is paused; your VNC session remains connected."
        : `Demo usage is paused: ${error.message}. Your VNC session remains connected.`
    );
    showToast("VNC remains connected; automatic usage tracking is paused.");
  } finally {
    state.usageReportBusy = false;
  }
}

function startConnectionAutomation() {
  if (!state.connectionActive || !navigator.onLine) return;
  $("qualityAutoStatus").textContent = "Automatically selecting the highest simulated download speed. No carrier network is changed.";
  $("automaticUsageStatus").textContent = "Automatic demo usage is running: 0.005 GB every 30 seconds while the app is open and connected.";
  if (!state.networkDemoTimer) {
    state.networkDemoIndex = 0;
    state.networkDemoTimer = setInterval(advanceNetworkDemo, 1900);
    advanceNetworkDemo();
  }
  if (!state.usageTimer) {
    if (state.availableBalanceGb > 0) {
      state.usageTimer = setInterval(reportAutomaticUsage, AUTO_USAGE_INTERVAL_MS);
      reportAutomaticUsage();
    } else {
      pauseAutomaticUsage("No data balance remains. Demo usage is paused; your VNC session remains connected.");
    }
  }
}

function updateBrowserNetwork() {
  if (!state.connectionActive || !state.selectedNetwork) {
    $("measurementValue").textContent = navigator.onLine ? "Online · browser state" : "Offline · browser state";
    $("measurementNote").textContent = "Actual radio type and speed are not exposed by this browser";
  }
  renderConnectionState();
}

function renderConnectionState() {
  const online = state.connectionActive && navigator.onLine;
  const statusText = online ? "Online" : "Offline";
  const connection = navigator.connection || navigator.mozConnection || navigator.webkitConnection;
  $("networkType").textContent = online
    ? state.selectedNetwork ? `${state.selectedNetwork.type} · fastest (demo)` : "Selecting fastest demo network…"
    : connection?.effectiveType ? `Browser effective: ${connection.effectiveType.toUpperCase()}` : "Browser status only";
  $("connectionStatusButton").classList.toggle("online", online);
  $("connectionStatusButton").classList.toggle("offline", !online);
  $("connectionStatusButton").setAttribute("aria-label", `VNC connection ${statusText.toLowerCase()}. Click to ${online ? "disconnect" : "connect"}`);
  $("headerConnectionStatus").textContent = state.connecting ? "Connecting" : statusText;
  $("connectionChip").textContent = state.connecting ? "Connecting" : statusText;
  $("connectionChip").classList.toggle("online", online);
  $("connectBtn").textContent = state.connecting ? "Connecting…" : online ? "Disconnect" : "Connect";
  $("connectBtn").disabled = state.connecting;
  $("connectBtn").setAttribute("aria-pressed", String(online));
  $("heroCard").classList.toggle("is-connecting", state.connecting);
  $("heroCard").classList.toggle("is-connected", online && !state.connecting);
  if (state.connecting) {
    $("connectionTitle").textContent = "Connecting to VNC";
    $("connectionDescription").textContent = "Starting your connection session…";
  } else if (online) {
    $("connectionTitle").textContent = "VNC session connected";
    $("connectionDescription").textContent = "Automatically selecting the fastest simulated 4G/5G candidate; no carrier network changed.";
  } else if (!navigator.onLine) {
    $("connectionTitle").textContent = "Device reports offline";
    $("connectionDescription").textContent = "Reconnect your device to the internet, then connect to VNC.";
  } else {
    $("connectionTitle").textContent = "Ready to connect";
    $("connectionDescription").textContent = "Connect to start your VNC session. Carrier switching is not enabled.";
  }
}

async function toggleConnection() {
  if (state.connecting) return;
  if (state.connectionActive) {
    stopConnectionAutomation();
    state.connectionActive = false;
    localStorage.removeItem("vnc_connection_active");
    localStorage.removeItem("vnc_simulated_route");
    renderConnectionState();
    showToast("Disconnected from VNC");
    return;
  }
  if (!navigator.onLine) {
    renderConnectionState();
    showToast("Your device is offline. Connect to the internet first.");
    return;
  }
  state.connecting = true;
  renderConnectionState();
  try {
    const startingNetwork = fastestNetwork(NETWORK_DEMO[0].networks);
    const result = await api("/api/routes/select", { method: "POST", body: JSON.stringify({ route: startingNetwork.route }) });
    await new Promise((resolve) => setTimeout(resolve, 850));
    $("currentRoute").textContent = result.route;
    $("selectedRouteName").textContent = result.route;
    $("routeExplanation").textContent = result.explanation;
    $("routeScore").textContent = "92";
    state.simulatedRoute = startingNetwork.route;
    localStorage.setItem("vnc_simulated_route", startingNetwork.route);
    state.connectionActive = true;
    localStorage.setItem("vnc_connection_active", "true");
    startConnectionAutomation();
    showToast(state.availableBalanceGb > 0
      ? "VNC session connected"
      : "VNC demo session connected. Add data to resume usage tracking.");
  } catch (error) {
    showToast(error.message);
  } finally {
    state.connecting = false;
    renderConnectionState();
  }
}

let authMode = "register";

function openAuth() {
  $("authModal").classList.add("open");
  setAuthMode("register");
}

function setAuthMode(mode) {
  authMode = mode;
  $("authTitle").textContent = mode === "register" ? "Welcome to VNC" : "Sign in to VNC";
  $("authSubtitle").textContent = mode === "register" ? "Create your secure account to begin." : "Use your registered email and password.";
  $("authSubmit").textContent = mode === "register" ? "Create account" : "Sign in";
  $("nameField").hidden = mode !== "register";
  $("authSwitch").textContent = mode === "register" ? "Already have an account? Sign in" : "New to VNC? Create account";
  $("authStatus").className = "inline-status";
  $("authStatus").textContent = "";
}

async function submitAuth(event) {
  event.preventDefault();
  const email = $("authEmail").value.trim();
  const password = $("authPassword").value;
  const name = $("authName").value.trim();
  try {
    const path = authMode === "register" ? "/api/auth/register" : "/api/auth/login";
    const body = authMode === "register" ? { name, email, password } : { email, password };
    const result = await api(path, { method: "POST", body: JSON.stringify(body) });
    state.token = result.token;
    state.user = result.user;
    localStorage.setItem("smartdata_token", state.token);
    $("authModal").classList.remove("open");
    await initializeSession();
  } catch (error) {
    $("authStatus").className = "inline-status show error";
    $("authStatus").textContent = error.message;
  }
}

async function initializeSession() {
  if (!state.token) {
    openAuth();
    return;
  }
  try {
    const profile = await api("/api/profile");
    state.user = profile;
    $("displayName").textContent = profile.name;
    $("adminBtn").hidden = profile.role !== "admin";
    await Promise.all([loadPlans(), loadDashboard()]);
    restoreSimulatedRoute();
    if (state.connectionActive && navigator.onLine) {
      startConnectionAutomation();
    } else if (state.connectionActive) {
      state.connectionActive = false;
      localStorage.removeItem("vnc_connection_active");
    }
    renderConnectionState();
  } catch (error) {
    localStorage.removeItem("smartdata_token");
    state.token = null;
    openAuth();
  }
}

async function logout() {
  try {
    await api("/api/auth/logout", { method: "POST" });
  } catch (_) {}
  localStorage.removeItem("smartdata_token");
  localStorage.removeItem("vnc_connection_active");
  state.token = null;
  state.user = null;
  state.connectionActive = false;
  stopConnectionAutomation();
  renderConnectionState();
  openAuth();
}

async function forgotPassword() {
  const email = $("authEmail").value.trim();
  if (!email) {
    $("authStatus").className = "inline-status show error";
    $("authStatus").textContent = "Enter your email first.";
    return;
  }
  await api("/api/auth/reset", { method: "POST", body: JSON.stringify({ email }) });
  $("authStatus").className = "inline-status show";
  $("authStatus").textContent = "If the account exists, a reset process can continue.";
}

document.querySelectorAll(".nav-item").forEach((button) => button.addEventListener("click", () => showPage(button.dataset.page)));
document.querySelectorAll("[data-page-target]").forEach((button) => button.addEventListener("click", () => showPage(button.dataset.pageTarget)));
$("buyDataBtn").addEventListener("click", () => showPage("plans"));
$("openConnectionBtn").addEventListener("click", () => showPage("route"));
$("connectBtn").addEventListener("click", toggleConnection);
$("connectionStatusButton").addEventListener("click", toggleConnection);
$("closeCheckout").addEventListener("click", () => $("checkoutModal").classList.remove("open"));
$("walletPayBtn").addEventListener("click", purchaseWithWallet);
$("interswitchPayBtn").addEventListener("click", initiateInterswitch);
$("walletTopupBtn").addEventListener("click", addWalletFunds);
$("demoCreditBtn").addEventListener("click", addDemoCredit);
$("authForm").addEventListener("submit", submitAuth);
$("togglePassword").addEventListener("click", () => {
  const showing = $("authPassword").type === "text";
  $("authPassword").type = showing ? "password" : "text";
  $("togglePassword").textContent = showing ? "Show" : "Hide";
  $("togglePassword").setAttribute("aria-label", showing ? "Show password" : "Hide password");
  $("togglePassword").setAttribute("aria-pressed", String(!showing));
});
$("authSwitch").addEventListener("click", () => setAuthMode(authMode === "register" ? "login" : "register"));
$("forgotPassword").addEventListener("click", forgotPassword);
$("logoutBtn").addEventListener("click", logout);
$("profileBtn").addEventListener("click", async () => {
  const name = prompt("Display name", state.user?.name || "");
  if (!name || name.trim().length < 2) return;
  try {
    await api("/api/profile", { method: "PUT", body: JSON.stringify({ name: name.trim() }) });
    state.user.name = name.trim();
    $("displayName").textContent = name.trim();
    showToast("Profile updated");
  } catch (error) {
    showToast(error.message);
  }
});
$("adminBtn").addEventListener("click", () => window.open("/admin.html", "_blank"));
$("notificationsBtn").addEventListener("click", () => showPage("usage"));
window.addEventListener("message", (event) => {
  if (event.origin !== window.location.origin || event.data?.type !== "vnc-payment-verification") return;
  if (event.data.status === "approved") {
    const successMessage = `Transaction successful. ${event.data.message || "Your VNC wallet has been updated."}`;
    $("checkoutStatus").className = "inline-status show";
    $("checkoutStatus").textContent = successMessage;
    showToast(successMessage);
    loadDashboard();
  } else if (event.data.status === "pending_verification" || event.data.status === "error") {
    $("checkoutStatus").className = "inline-status show error";
    $("checkoutStatus").textContent = event.data.message || "Payment verification is still pending.";
  } else if (event.data.status === "failed") {
    $("checkoutStatus").className = "inline-status show error";
    $("checkoutStatus").textContent = event.data.message || "Payment failed or was cancelled.";
  }
});

window.addEventListener("online", updateBrowserNetwork);
window.addEventListener("offline", () => {
  stopConnectionAutomation();
  if (state.connectionActive) {
    state.connectionActive = false;
    localStorage.removeItem("vnc_connection_active");
    localStorage.removeItem("vnc_simulated_route");
  }
  updateBrowserNetwork();
});
updateBrowserNetwork();
initializeSession();

if ("serviceWorker" in navigator && location.protocol.startsWith("http")) {
  window.addEventListener("load", () => navigator.serviceWorker.register("/sw.js").catch(() => {}));
}
