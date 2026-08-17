import { initializeApp } from "https://www.gstatic.com/firebasejs/12.2.0/firebase-app.js";
import {
  GoogleAuthProvider,
  getAuth,
  onAuthStateChanged,
  sendPasswordResetEmail,
  signInWithEmailAndPassword,
  signInWithPopup,
  signOut,
} from "https://www.gstatic.com/firebasejs/12.2.0/firebase-auth.js";
import {
  Timestamp,
  addDoc,
  collection,
  doc,
  getDocs,
  getFirestore,
  query,
  serverTimestamp,
  setDoc,
  where,
} from "https://www.gstatic.com/firebasejs/12.2.0/firebase-firestore.js";

const login = document.querySelector("[data-login]");
const dashboard = document.querySelector("[data-dashboard]");
const adminHeader = document.querySelector("[data-admin-header]");
const loginForm = document.querySelector("[data-login-form]");
const loginMessage = document.querySelector("[data-login-message]");
const emailSignInButton = document.querySelector("[data-email-sign-in]");
const googleSignInButton = document.querySelector("[data-google-sign-in]");
const passwordResetButton = document.querySelector("[data-password-reset]");
const statusLine = document.querySelector("[data-status]");
const signOutButton = document.querySelector("[data-sign-out]");
const refreshButton = document.querySelector("[data-refresh]");
const metricsNode = document.querySelector("[data-metrics]");
const installationRows = document.querySelector("[data-installations]");
const leadsNode = document.querySelector("[data-leads]");
const entitlementDialog = document.querySelector("[data-entitlement-dialog]");
const entitlementForm = document.querySelector("[data-entitlement-form]");
const entitlementTitle = document.querySelector("[data-entitlement-title]");
const entitlementMessage = document.querySelector("[data-entitlement-message]");
const entitlementCloseButton = document.querySelector("[data-entitlement-close]");
const entitlementSaveButton = document.querySelector("[data-entitlement-save]");

let auth;
let database;
let installations = [];
let entitlements = new Map();

const setLoginMessage = (message, tone = "error") => {
  loginMessage.textContent = message;
  loginMessage.dataset.tone = message ? tone : "";
};

const setAuthBusy = (busy) => {
  emailSignInButton.disabled = busy;
  googleSignInButton.disabled = busy;
  passwordResetButton.disabled = busy;
};

const escapeHtml = (value) =>
  String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");

const dateValue = (value) => {
  if (!value) return null;
  if (typeof value.toDate === "function") return value.toDate();
  const parsed = new Date(value);
  return Number.isNaN(parsed.valueOf()) ? null : parsed;
};

const formatDate = (value) => {
  const date = dateValue(value);
  if (!date) return "Not yet";
  return new Intl.DateTimeFormat("en", {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(date);
};

const toLocalInput = (value) => {
  const date = dateValue(value);
  if (!date) return "";
  const offset = date.getTimezoneOffset() * 60_000;
  return new Date(date.valueOf() - offset).toISOString().slice(0, 16);
};

const currentEntitlementStatus = (record) => {
  if (!record) return "Not approved";
  const now = new Date();
  const endsAt = dateValue(record.ends_at);
  const graceEndsAt = dateValue(record.grace_ends_at);
  if (["trial", "active"].includes(record.status)) {
    return !endsAt || endsAt > now ? record.status : "expired";
  }
  if (record.status === "grace" && graceEndsAt && graceEndsAt > now) {
    return "grace";
  }
  return record.status;
};

const loadCollection = async (name, constraint) => {
  const reference = collection(database, name);
  const snapshot = await getDocs(
    constraint ? query(reference, constraint) : reference,
  );
  return snapshot.docs.map((item) => ({ id: item.id, ...item.data() }));
};

const loadLeads = async () => {
  const token = await auth.currentUser.getIdToken();
  const response = await fetch("/api/admin/leads", {
    headers: { Authorization: `Bearer ${token}` },
    cache: "no-store",
  });
  if (!response.ok) {
    throw new Error("Access requests could not be loaded");
  }
  const payload = await response.json();
  return Array.isArray(payload.leads) ? payload.leads : [];
};

const renderMetrics = (leads, usage) => {
  const now = new Date();
  const activeBoundary = new Date(now.valueOf() - 30 * 24 * 60 * 60 * 1000);
  const installed = installations.filter((item) => item.status === "installed").length;
  const activated = installations.filter((item) => dateValue(item.first_check_at)).length;
  const monthlyActive = installations.filter(
    (item) => (dateValue(item.recent_check_at) ?? new Date(0)) >= activeBoundary,
  ).length;
  const paid = Array.from(entitlements.values()).filter(
    (item) => item.billing_status === "paid" &&
      ["trial", "active", "grace"].includes(currentEntitlementStatus(item)),
  ).length;
  const checks = usage.reduce((sum, item) => sum + Number(item.checks_started || 0), 0);
  const values = [
    ["Leads", leads.length],
    ["Installed accounts", installed],
    ["Activated accounts", activated],
    ["Monthly active", monthlyActive],
    ["Paid customers", paid],
  ];
  metricsNode.innerHTML = values
    .map(
      ([label, value]) =>
        `<article class="metric-card"><span>${escapeHtml(label)}</span>` +
        `<strong>${escapeHtml(value)}</strong></article>`,
    )
    .join("");
  statusLine.textContent = `${checks} checks started this month. Updated ${formatDate(now)}.`;
};

const renderInstallations = (usage) => {
  const usageByInstallation = new Map(
    usage.map((item) => [String(item.installation_id), item]),
  );
  const sorted = [...installations].sort(
    (left, right) =>
      (dateValue(right.installed_at)?.valueOf() || 0) -
      (dateValue(left.installed_at)?.valueOf() || 0),
  );
  installationRows.innerHTML = sorted.length
    ? sorted
        .map((item) => {
          const entitlement = entitlements.get(String(item.installation_id));
          const productStatus = currentEntitlementStatus(entitlement);
          const inactive = !["active", "trial", "grace"].includes(productStatus);
          const usageItem = usageByInstallation.get(String(item.installation_id));
          return (
            "<tr>" +
            `<td><span class="account-name">${escapeHtml(item.account_login || "Unknown")}</span>` +
            `<span class="account-type">${escapeHtml(item.account_type || "Unknown")}</span></td>` +
            `<td>${escapeHtml(item.installation_id)}</td>` +
            `<td><span class="status-pill${inactive ? " is-inactive" : ""}">${escapeHtml(productStatus)}</span></td>` +
            `<td>${escapeHtml(usageItem?.checks_started || 0)}</td>` +
            `<td>${escapeHtml(formatDate(item.recent_check_at))}</td>` +
            `<td><button class="secondary-button" data-manage="${escapeHtml(item.installation_id)}">Manage</button></td>` +
            "</tr>"
          );
        })
        .join("")
    : '<tr><td colspan="6" class="muted">No GitHub installations have been recorded yet.</td></tr>';

  installationRows.querySelectorAll("[data-manage]").forEach((button) => {
    button.addEventListener("click", () => openEntitlement(button.dataset.manage));
  });
};

const renderLeads = (leads) => {
  const sorted = [...leads].sort(
    (left, right) => new Date(right.last_seen) - new Date(left.last_seen),
  );
  leadsNode.innerHTML = sorted.length
    ? sorted
        .map(
          (lead) =>
            '<article class="lead-card">' +
            `<strong>${escapeHtml(lead.name || lead.email)}</strong>` +
            `<p><a href="mailto:${escapeHtml(lead.email)}">${escapeHtml(lead.email)}</a></p>` +
            `<p class="muted">${escapeHtml(lead.company || "No company provided")}</p>` +
            `<p class="message">${escapeHtml(lead.last_message || "No message provided")}</p>` +
            `<p class="account-type">Last request ${escapeHtml(formatDate(lead.last_seen))}</p>` +
            "</article>",
        )
        .join("")
    : '<p class="muted">No access requests have been received yet.</p>';
};

const loadDashboard = async () => {
  statusLine.textContent = "Loading live records";
  refreshButton.disabled = true;
  try {
    const period = new Date().toISOString().slice(0, 7);
    const [installationItems, entitlementItems, usageItems, leads] = await Promise.all([
      loadCollection("fraeno_installations"),
      loadCollection("fraeno_entitlements"),
      loadCollection("fraeno_usage", where("period", "==", period)),
      loadLeads(),
    ]);
    installations = installationItems;
    entitlements = new Map(
      entitlementItems.map((item) => [String(item.installation_id), item]),
    );
    const currentUsage = usageItems.filter((item) => item.period === period);
    renderMetrics(leads, currentUsage);
    renderInstallations(currentUsage);
    renderLeads(leads);
  } catch (error) {
    statusLine.textContent = error instanceof Error ? error.message : "Records could not be loaded";
  } finally {
    refreshButton.disabled = false;
  }
};

const openEntitlement = (installationId) => {
  const installation = installations.find(
    (item) => String(item.installation_id) === String(installationId),
  );
  const record = entitlements.get(String(installationId));
  entitlementForm.reset();
  entitlementForm.elements.installation_id.value = installationId;
  entitlementForm.elements.status.value = record?.status || "trial";
  entitlementForm.elements.plan.value = record?.plan || "private_beta";
  entitlementForm.elements.source.value = record?.source || "manual";
  entitlementForm.elements.billing_status.value = record?.billing_status || "unpaid";
  entitlementForm.elements.ends_at.value = toLocalInput(record?.ends_at);
  entitlementForm.elements.grace_ends_at.value = toLocalInput(record?.grace_ends_at);
  entitlementForm.elements.note.value = record?.note || "";
  entitlementTitle.textContent = installation?.account_login || `Installation ${installationId}`;
  entitlementMessage.textContent = "";
  entitlementDialog.showModal();
};

const optionalTimestamp = (value) =>
  value ? Timestamp.fromDate(new Date(value)) : null;

const saveEntitlement = async (event) => {
  event.preventDefault();
  if (event.submitter !== entitlementSaveButton) return;
  const user = auth.currentUser;
  if (!user) return;
  const formData = new FormData(entitlementForm);
  const installationId = String(formData.get("installation_id"));
  const payload = {
    installation_id: Number(installationId),
    status: String(formData.get("status")),
    plan: String(formData.get("plan")),
    source: String(formData.get("source")),
    billing_status: String(formData.get("billing_status")),
    starts_at: entitlements.get(installationId)?.starts_at || serverTimestamp(),
    ends_at: optionalTimestamp(String(formData.get("ends_at") || "")),
    grace_ends_at: optionalTimestamp(String(formData.get("grace_ends_at") || "")),
    note: String(formData.get("note")),
    updated_at: serverTimestamp(),
    updated_by: user.email || user.uid,
  };
  entitlementMessage.textContent = "Saving";
  try {
    await setDoc(doc(database, "fraeno_entitlements", installationId), payload, {
      merge: true,
    });
    await addDoc(collection(database, "fraeno_admin_audit"), {
      action: "entitlement_updated",
      installation_id: Number(installationId),
      status: payload.status,
      plan: payload.plan,
      billing_status: payload.billing_status,
      actor: payload.updated_by,
      note: payload.note,
      created_at: serverTimestamp(),
    });
    entitlementDialog.close();
    await loadDashboard();
  } catch (error) {
    entitlementMessage.textContent =
      error instanceof Error ? error.message : "The entitlement could not be saved";
  }
};

const initialize = async () => {
  try {
    const response = await fetch("/api/admin/config", { cache: "no-store" });
    if (!response.ok) throw new Error("Admin authentication is not configured");
    const config = await response.json();
    const app = initializeApp(config);
    auth = getAuth(app);
    database = getFirestore(app);
    setAuthBusy(false);
    onAuthStateChanged(auth, async (user) => {
      if (!user) {
        login.hidden = false;
        dashboard.hidden = true;
        adminHeader.hidden = true;
        signOutButton.hidden = true;
        return;
      }
      const token = await user.getIdTokenResult(true);
      if (token.claims.isAdmin !== true) {
        setLoginMessage("This account does not have admin access.");
        await signOut(auth);
        return;
      }
      login.hidden = true;
      dashboard.hidden = false;
      adminHeader.hidden = false;
      signOutButton.hidden = false;
      await loadDashboard();
    });
  } catch (error) {
    setLoginMessage(
      error instanceof Error ? error.message : "Admin authentication could not start",
    );
  }
};

loginForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  setAuthBusy(true);
  setLoginMessage("Signing in", "progress");
  const formData = new FormData(loginForm);
  try {
    await signInWithEmailAndPassword(
      auth,
      String(formData.get("email")),
      String(formData.get("password")),
    );
    setLoginMessage("");
  } catch {
    setLoginMessage("Sign in failed. Check your account and try again.");
  } finally {
    setAuthBusy(false);
  }
});

googleSignInButton.addEventListener("click", async () => {
  setAuthBusy(true);
  setLoginMessage("Opening Google sign in", "progress");
  try {
    await signInWithPopup(auth, new GoogleAuthProvider());
    setLoginMessage("");
  } catch {
    setLoginMessage("Google sign in did not complete. Please try again.");
  } finally {
    setAuthBusy(false);
  }
});

passwordResetButton.addEventListener("click", async () => {
  const email = String(loginForm.elements.email.value || "").trim();
  setLoginMessage("");
  if (!email) {
    setLoginMessage("Enter your email address to reset your password.");
    loginForm.elements.email.focus();
    return;
  }

  setAuthBusy(true);
  passwordResetButton.textContent = "Sending reset link...";
  try {
    await sendPasswordResetEmail(auth, email);
    setLoginMessage(
      "If an account exists for this email, a password reset link has been sent.",
      "success",
    );
  } catch {
    setLoginMessage("We could not send a reset link right now. Please try again.");
  } finally {
    passwordResetButton.textContent = "Forgot password?";
    setAuthBusy(false);
  }
});

signOutButton.addEventListener("click", () => signOut(auth));
refreshButton.addEventListener("click", loadDashboard);
entitlementCloseButton.addEventListener("click", () => entitlementDialog.close());
entitlementForm.addEventListener("submit", saveEntitlement);

initialize();
