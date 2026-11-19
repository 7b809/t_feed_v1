(() => {
  const fab = document.getElementById("mobileFab");
  const menu = document.getElementById("mobileFabMenu");
  if (fab && menu) fab.addEventListener("click", () => menu.classList.toggle("show"));
  const toggle = document.getElementById("mobileNavToggle");
  if (toggle && menu) toggle.addEventListener("click", () => menu.classList.toggle("show"));
  window.showToast = (message, type = "info") => {
    const area = document.getElementById("toastArea");
    if (!area) return;
    const node = document.createElement("div");
    node.className = "app-toast";
    node.textContent = message;
    if (type === "error") node.style.borderLeft = "3px solid #ef737b";
    if (type === "success") node.style.borderLeft = "3px solid #28c58a";
    area.appendChild(node);
    setTimeout(() => node.remove(), 4200);
  };
  window.apiRequest = async (url, options = {}) => {
    const response = await fetch(url, {
      ...options,
      headers: {"Content-Type": "application/json", ...(options.headers || {})}
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) {
      const detail = payload.detail;
      throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail || response.statusText));
    }
    return payload;
  };
  window.escapeHtml = value => String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
})();