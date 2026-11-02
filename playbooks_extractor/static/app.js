(() => {
  const state = {
    books: new Map(),
    dataDir: "/data",
    filter: "extracted",
    search: "",
    authExpanded: true,
    scheduleExpanded: true,
    userPickedFilter: false,
  };

  const els = {
    sseIndicator: document.getElementById("sse-indicator"),
    sseText: document.getElementById("sse-text"),
    dataDirLabel: document.getElementById("data-dir-label"),
    toast: document.getElementById("toast-banner"),
    statExtracted: document.getElementById("stat-extracted"),
    statExporting: document.getElementById("stat-exporting"),
    statAvailable: document.getElementById("stat-available"),
    statTotal: document.getElementById("stat-total"),
    authBadge: document.getElementById("auth-badge"),
    authMessage: document.getElementById("auth-message"),
    authBody: document.getElementById("auth-body"),
    toggleAuthBtn: document.getElementById("toggle-auth-btn"),
    cookieForm: document.getElementById("cookie-form"),
    cookieFile: document.getElementById("cookie-file"),
    cookieTextarea: document.getElementById("cookie-textarea"),
    saveCookiesBtn: document.getElementById("save-cookies-btn"),
    verifyCookiesBtn: document.getElementById("verify-cookies-btn"),
    clearCookiesBtn: document.getElementById("clear-cookies-btn"),
    scheduleBadge: document.getElementById("schedule-badge"),
    scheduleBody: document.getElementById("schedule-body"),
    toggleScheduleBtn: document.getElementById("toggle-schedule-btn"),
    scheduleForm: document.getElementById("schedule-form"),
    scheduleEnabled: document.getElementById("schedule-enabled"),
    scheduleCronInput: document.getElementById("schedule-cron-input"),
    saveScheduleBtn: document.getElementById("save-schedule-btn"),
    scheduleValidationMsg: document.getElementById("schedule-validation-msg"),
    scheduleNextRun: document.getElementById("schedule-next-run"),
    scheduleLastRun: document.getElementById("schedule-last-run"),
    cronPresetBtns: Array.from(document.querySelectorAll(".cron-preset-btn")),
    activeSection: document.getElementById("active-exports-section"),
    activeBadge: document.getElementById("active-exports-badge"),
    activeList: document.getElementById("active-exports-list"),
    syncLibraryBtn: document.getElementById("sync-library-btn"),
    exportAllBtn: document.getElementById("export-all-btn"),
    reconcileBtn: document.getElementById("reconcile-btn"),
    tabCountExtracted: document.getElementById("tab-count-extracted"),
    tabCountExporting: document.getElementById("tab-count-exporting"),
    tabCountAll: document.getElementById("tab-count-all"),
    tabCountAvailable: document.getElementById("tab-count-available"),
    searchInput: document.getElementById("search-input"),
    booksEmpty: document.getElementById("books-empty"),
    booksEmptyText: document.getElementById("books-empty-text"),
    booksList: document.getElementById("books-list"),
    tabs: Array.from(document.querySelectorAll(".tab")),
  };

  let toastTimer = null;
  function showToast(message, isError = false) {
    els.toast.textContent = message;
    els.toast.classList.toggle("toast-error", isError);
    els.toast.classList.remove("hidden");
    if (toastTimer) clearTimeout(toastTimer);
    toastTimer = setTimeout(() => {
      els.toast.classList.add("hidden");
    }, 6000);
  }

  function formatBytes(bytes) {
    if (!bytes || bytes <= 0) return "0 B";
    const units = ["B", "KB", "MB", "GB"];
    let idx = 0;
    let val = bytes;
    while (val >= 1024 && idx < units.length - 1) {
      val /= 1024;
      idx += 1;
    }
    return `${val.toFixed(idx >= 2 ? 1 : 0)} ${units[idx]}`;
  }

  function formatStage(stage, downloaded, total) {
    const labels = {
      queued: "Queued for export…",
      resolving: "Resolving export link…",
      downloading_audio: "Downloading audio (.m4a)…",
      downloading_pdf: "Downloading accompanying PDF…",
      completed: "Extracted",
      failed: "Export failed",
      restricted: "Publisher restricted",
    };
    const base = labels[stage] || stage || "Working…";
    if (downloaded > 0 && total > 0) {
      return `${base} (${formatBytes(downloaded)} / ${formatBytes(total)})`;
    }
    if (downloaded > 0) {
      return `${base} (${formatBytes(downloaded)})`;
    }
    return base;
  }

  function escapeHtml(str) {
    const div = document.createElement("div");
    div.textContent = str ?? "";
    return div.innerHTML;
  }

  function setAuthPanelExpanded(expanded) {
    state.authExpanded = expanded;
    els.authBody.classList.toggle("hidden", !expanded);
    els.toggleAuthBtn.textContent = expanded ? "Hide Panel" : "Configure Auth";
  }

  function setSchedulePanelExpanded(expanded) {
    state.scheduleExpanded = expanded;
    els.scheduleBody.classList.toggle("hidden", !expanded);
    els.toggleScheduleBtn.textContent = expanded ? "Hide Panel" : "Configure Schedule";
  }

  function formatTimestamp(isoStr) {
    if (!isoStr) return "—";
    const dt = new Date(isoStr);
    if (Number.isNaN(dt.getTime())) return isoStr;
    return dt.toLocaleString(undefined, {
      year: "numeric",
      month: "short",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
    });
  }

  function renderSchedule(payload, updateInputs = false) {
    if (!payload) return;
    if (updateInputs) {
      els.scheduleEnabled.checked = Boolean(payload.enabled);
      els.scheduleCronInput.value = payload.cron_expression || "0 2 * * *";
      els.scheduleCronInput.classList.remove("invalid");
      els.scheduleValidationMsg.classList.remove("invalid");
      els.scheduleValidationMsg.textContent = payload.description
        ? `✓ Valid crontab — ${payload.description}`
        : "";
    }

    if (payload.is_running) {
      els.scheduleBadge.className = "pill pill-info";
      els.scheduleBadge.textContent = "Running Scheduled Sync…";
    } else if (payload.enabled) {
      els.scheduleBadge.className = "pill pill-success";
      els.scheduleBadge.textContent = `Active · ${payload.description || payload.cron_expression}`;
    } else {
      els.scheduleBadge.className = "pill pill-muted";
      els.scheduleBadge.textContent = "Disabled";
    }

    const tzSuffix = payload.timezone ? ` (${payload.timezone})` : "";
    els.scheduleNextRun.textContent = payload.enabled && payload.next_run_at
      ? `${formatTimestamp(payload.next_run_at)}${tzSuffix}`
      : "Disabled";

    if (payload.last_run_at) {
      els.scheduleLastRun.textContent = `${formatTimestamp(payload.last_run_at)} — ${payload.last_message || payload.last_status}`;
    } else {
      els.scheduleLastRun.textContent = payload.last_message || "Scheduled sync has not run yet.";
    }
  }

  let cronValidateTimer = null;
  async function validateCronInputLive() {
    const raw = els.scheduleCronInput.value.trim();
    if (!raw) {
      els.scheduleCronInput.classList.add("invalid");
      els.scheduleValidationMsg.classList.add("invalid");
      els.scheduleValidationMsg.textContent =
        "✗ Crontab schedule cannot be empty (e.g. '0 2 * * *').";
      return false;
    }
    try {
      const res = await apiFetch("/api/schedule/validate", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ cron_expression: raw }),
      });
      if (res.valid) {
        els.scheduleCronInput.classList.remove("invalid");
        els.scheduleValidationMsg.classList.remove("invalid");
        const nextPreview = res.next_run_at ? ` · Next: ${formatTimestamp(res.next_run_at)}` : "";
        els.scheduleValidationMsg.textContent = `✓ Valid crontab — ${res.description}${nextPreview}`;
        return true;
      }
      els.scheduleCronInput.classList.add("invalid");
      els.scheduleValidationMsg.classList.add("invalid");
      els.scheduleValidationMsg.textContent = `✗ ${res.error}`;
      return false;
    } catch (err) {
      els.scheduleCronInput.classList.add("invalid");
      els.scheduleValidationMsg.classList.add("invalid");
      els.scheduleValidationMsg.textContent = `✗ ${err.message}`;
      return false;
    }
  }

  function renderAuth(payload) {
    const ver = payload.verification || {};
    if (ver.authenticated) {
      els.authBadge.className = "pill pill-success";
      els.authBadge.textContent = ver.account_hint
        ? `Authenticated (${ver.account_hint})`
        : "Authenticated";
    } else if (payload.has_cookies) {
      els.authBadge.className = "pill pill-warning";
      els.authBadge.textContent = `Cookies Saved (${payload.cookie_count}) — Unverified`;
    } else {
      els.authBadge.className = "pill pill-warning";
      els.authBadge.textContent = "Not Authenticated";
    }
    els.authMessage.textContent =
      ver.message ||
      (payload.has_cookies
        ? `${payload.cookie_count} Google cookies stored in /config/cookies.json.`
        : "No session cookies imported yet.");
  }

  function upsertBooksList(books, dataDir) {
    if (dataDir) {
      state.dataDir = dataDir;
      els.dataDirLabel.textContent = dataDir;
    }
    state.books.clear();
    for (const b of books) {
      state.books.set(b.volume_id, b);
    }
    // Automatically switch initial view to All Library if nothing extracted yet but books exist
    if (!state.userPickedFilter) {
      const hasExtracted = books.some((b) => b.status === "extracted");
      if (!hasExtracted && books.length > 0) {
        setActiveTab("all", false);
      } else if (hasExtracted) {
        setActiveTab("extracted", false);
      }
    }
    renderLibrary();
  }

  function upsertSingleBook(book) {
    state.books.set(book.volume_id, book);
    renderLibrary();
  }

  function setActiveTab(filter, userInitiated = true) {
    state.filter = filter;
    if (userInitiated) state.userPickedFilter = true;
    for (const tab of els.tabs) {
      tab.classList.toggle("active", tab.dataset.filter === filter);
    }
    renderLibrary();
  }

  function renderProgressBlock(book) {
    const pct = Number(book.progress_percent || 0).toFixed(1);
    const stageText = formatStage(book.stage, book.downloaded_bytes, book.total_bytes);
    return `
      <div class="progress-wrap">
        <div class="progress-header">
          <span>${escapeHtml(stageText)}</span>
          <span class="progress-pct">${pct}%</span>
        </div>
        <div class="progress-track" role="progressbar" aria-valuenow="${pct}" aria-valuemin="0" aria-valuemax="100">
          <div class="progress-fill" style="width: ${Math.min(100, Math.max(0, Number(pct)))}%"></div>
        </div>
      </div>
    `;
  }

  function renderBookCard(book, compactActive = false) {
    const initials = (book.title || "AB").trim().slice(0, 2).toUpperCase();
    const coverHtml = book.cover_url
      ? `<img class="book-cover" src="${escapeHtml(book.cover_url)}" alt="" loading="lazy" />`
      : `<div class="book-cover" aria-hidden="true">${escapeHtml(initials)}</div>`;

    const isActive = book.status === "exporting" || book.status === "queued";
    const isExtracted = book.status === "extracted";

    let badgesHtml = "";
    if (isExtracted) {
      const audioHref = `/api/books/${encodeURIComponent(book.volume_id)}/files/audio`;
      const pdfHref = `/api/books/${encodeURIComponent(book.volume_id)}/files/pdf`;
      badgesHtml = `
        <div class="artifact-badges">
          <span class="artifact-link" title="Folder inside ${escapeHtml(state.dataDir)}">
            📁 <code>${escapeHtml(book.folder_name)}/</code>
          </span>
          <a class="artifact-link" href="${audioHref}" target="_blank" rel="noopener">
            🎧 ${escapeHtml(book.audio_file)} (${formatBytes(book.audio_size_bytes)})
          </a>
          ${
            book.pdf_file
              ? `<a class="artifact-link" href="${pdfHref}" target="_blank" rel="noopener">
                  📄 ${escapeHtml(book.pdf_file)} (${formatBytes(book.pdf_size_bytes)})
                 </a>`
              : ""
          }
        </div>
      `;
    } else if (book.has_pdf) {
      badgesHtml = `
        <div class="artifact-badges">
          <span class="pill pill-outline">📄 Accompanying PDF detected</span>
        </div>
      `;
    }

    let noteHtml = "";
    if (book.error_message) {
      const errClass = book.status === "failed" ? "book-note error" : "book-note";
      noteHtml = `<div class="${errClass}">${escapeHtml(book.error_message)}</div>`;
    }

    let rightHtml = "";
    if (isActive) {
      rightHtml = renderProgressBlock(book);
    } else if (compactActive) {
      rightHtml = "";
    } else if (!book.export_supported || book.status === "restricted") {
      rightHtml = `<span class="pill pill-danger">Publisher Restricted</span>`;
    } else if (isExtracted) {
      rightHtml = `
        <span class="pill pill-success">Extracted (100%)</span>
        <button type="button" class="btn btn-ghost btn-sm export-btn" data-id="${escapeHtml(book.volume_id)}">
          Re-export
        </button>
      `;
    } else if (book.status === "failed") {
      rightHtml = `
        <span class="pill pill-danger">Failed</span>
        <button type="button" class="btn btn-primary btn-sm export-btn" data-id="${escapeHtml(book.volume_id)}">
          Retry Export
        </button>
      `;
    } else {
      rightHtml = `
        <button type="button" class="btn btn-primary btn-sm export-btn" data-id="${escapeHtml(book.volume_id)}">
          Export Audiobook
        </button>
      `;
    }

    return `
      <article class="book-item" data-volume-id="${escapeHtml(book.volume_id)}">
        <div class="book-main">
          ${coverHtml}
          <div class="book-details">
            <div class="book-title-line">
              <span class="book-title">${escapeHtml(book.title)}</span>
              <code class="volume-id">[${escapeHtml(book.volume_id)}]</code>
            </div>
            ${book.authors ? `<div class="book-author">${escapeHtml(book.authors)}</div>` : ""}
            ${badgesHtml}
            ${noteHtml}
          </div>
        </div>
        <div class="book-actions">
          ${rightHtml}
        </div>
      </article>
    `;
  }

  function renderLibrary() {
    const allBooks = Array.from(state.books.values());
    const extracted = allBooks.filter((b) => b.status === "extracted");
    const active = allBooks.filter((b) => b.status === "exporting" || b.status === "queued");
    const available = allBooks.filter(
      (b) => b.export_supported && (b.status === "available" || b.status === "failed")
    );

    els.statExtracted.textContent = String(extracted.length);
    els.statExporting.textContent = String(active.length);
    els.statAvailable.textContent = String(available.length);
    els.statTotal.textContent = String(allBooks.length);

    els.tabCountExtracted.textContent = String(extracted.length);
    els.tabCountExporting.textContent = String(active.length);
    els.tabCountAll.textContent = String(allBooks.length);
    els.tabCountAvailable.textContent = String(available.length);

    // Active exports top live card
    if (active.length > 0) {
      els.activeSection.classList.remove("hidden");
      els.activeBadge.textContent = `${active.length} active`;
      els.activeList.innerHTML = active.map((b) => renderBookCard(b, true)).join("");
    } else {
      els.activeSection.classList.add("hidden");
      els.activeList.innerHTML = "";
    }

    // Filtered main list
    const query = state.search.trim().toLowerCase();
    const filtered = allBooks.filter((b) => {
      if (state.filter === "extracted" && b.status !== "extracted") return false;
      if (state.filter === "exporting" && b.status !== "exporting" && b.status !== "queued")
        return false;
      if (
        state.filter === "available" &&
        !(b.export_supported && (b.status === "available" || b.status === "failed"))
      )
        return false;
      if (!query) return true;
      return (
        (b.title || "").toLowerCase().includes(query) ||
        (b.authors || "").toLowerCase().includes(query) ||
        (b.volume_id || "").toLowerCase().includes(query)
      );
    });

    if (filtered.length === 0) {
      els.booksEmpty.classList.remove("hidden");
      els.booksList.innerHTML = "";
      if (state.filter === "extracted" && allBooks.length > 0) {
        els.booksEmptyText.textContent =
          "No audiobooks extracted yet. Switch to 'All Library' or click 'Export All Available' to begin exporting.";
      } else if (state.filter === "exporting") {
        els.booksEmptyText.textContent = "No audiobooks are currently being exported.";
      } else {
        els.booksEmptyText.textContent =
          "No audiobooks match the current view. Import your Google Play Books cookies and click 'Sync Google Play Library'.";
      }
    } else {
      els.booksEmpty.classList.add("hidden");
      els.booksList.innerHTML = filtered.map((b) => renderBookCard(b, false)).join("");
    }
  }

  async function apiFetch(url, options = {}) {
    const resp = await fetch(url, options);
    let data = {};
    try {
      data = await resp.json();
    } catch (_err) {
      data = {};
    }
    if (!resp.ok) {
      throw new Error(data.detail || `HTTP ${resp.status}`);
    }
    return data;
  }

  async function loadInitialState() {
    try {
      const [auth, booksPayload, schedulePayload] = await Promise.all([
        apiFetch("/api/auth/status"),
        apiFetch("/api/books"),
        apiFetch("/api/schedule"),
      ]);
      renderAuth(auth);
      if (auth.verification && auth.verification.authenticated) {
        setAuthPanelExpanded(false);
      }
      renderSchedule(schedulePayload, true);
      upsertBooksList(booksPayload.books || [], booksPayload.data_dir);
    } catch (err) {
      showToast(`Failed loading initial state: ${err.message}`, true);
    }
  }

  function connectSSE() {
    const source = new EventSource("/api/events");

    source.onopen = () => {
      els.sseIndicator.className = "pill pill-success";
      els.sseText.textContent = "Live updates active";
    };

    source.onerror = () => {
      els.sseIndicator.className = "pill pill-warning";
      els.sseText.textContent = "Reconnecting…";
    };

    source.addEventListener("auth_updated", (ev) => {
      try {
        renderAuth(JSON.parse(ev.data));
      } catch (_e) {}
    });

    source.addEventListener("book_progress", (ev) => {
      try {
        upsertSingleBook(JSON.parse(ev.data));
      } catch (_e) {}
    });

    source.addEventListener("book_extracted", (ev) => {
      try {
        const book = JSON.parse(ev.data);
        upsertSingleBook(book);
        showToast(`Extracted "${book.title}" [${book.volume_id}]`);
      } catch (_e) {}
    });

    source.addEventListener("library_synced", (ev) => {
      try {
        const data = JSON.parse(ev.data);
        if (Array.isArray(data.books)) {
          upsertBooksList(data.books, state.dataDir);
        }
      } catch (_e) {}
    });

    source.addEventListener("library_updated", (ev) => {
      try {
        const data = JSON.parse(ev.data);
        if (Array.isArray(data.books)) {
          upsertBooksList(data.books, data.data_dir);
        }
      } catch (_e) {}
    });

    source.addEventListener("schedule_updated", (ev) => {
      try {
        renderSchedule(JSON.parse(ev.data), false);
      } catch (_e) {}
    });
  }

  // Event listeners
  els.toggleAuthBtn.addEventListener("click", () => {
    setAuthPanelExpanded(!state.authExpanded);
  });

  els.toggleScheduleBtn.addEventListener("click", () => {
    setSchedulePanelExpanded(!state.scheduleExpanded);
  });

  els.scheduleCronInput.addEventListener("input", () => {
    if (cronValidateTimer) clearTimeout(cronValidateTimer);
    cronValidateTimer = setTimeout(() => {
      validateCronInputLive();
    }, 220);
  });

  for (const presetBtn of els.cronPresetBtns) {
    presetBtn.addEventListener("click", () => {
      els.scheduleCronInput.value = presetBtn.dataset.cron || "0 2 * * *";
      validateCronInputLive();
    });
  }

  els.scheduleForm.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const raw = els.scheduleCronInput.value.trim();
    const enabled = Boolean(els.scheduleEnabled.checked);
    const ok = await validateCronInputLive();
    if (!ok) {
      showToast("Fix the crontab expression before saving.", true);
      return;
    }
    els.saveScheduleBtn.disabled = true;
    try {
      const updated = await apiFetch("/api/schedule", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ enabled, cron_expression: raw }),
      });
      renderSchedule(updated, true);
      showToast(
        updated.enabled
          ? `Schedule saved (${updated.description}).`
          : "Scheduled sync disabled."
      );
    } catch (err) {
      els.scheduleCronInput.classList.add("invalid");
      els.scheduleValidationMsg.classList.add("invalid");
      els.scheduleValidationMsg.textContent = `✗ ${err.message}`;
      showToast(`Failed saving schedule: ${err.message}`, true);
    } finally {
      els.saveScheduleBtn.disabled = false;
    }
  });

  els.cookieForm.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const file = els.cookieFile.files && els.cookieFile.files[0];
    const pasted = els.cookieTextarea.value.trim();
    if (!file && !pasted) {
      showToast("Select a cookies file or paste cookies/cURL text first.", true);
      return;
    }

    els.saveCookiesBtn.disabled = true;
    try {
      let auth;
      if (file) {
        const fd = new FormData();
        fd.append("file", file);
        fd.append("verify", "true");
        auth = await apiFetch("/api/auth/cookies", { method: "POST", body: fd });
      } else {
        auth = await apiFetch("/api/auth/cookies", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ raw_cookies: pasted, verify: true }),
        });
      }
      els.cookieTextarea.value = "";
      els.cookieFile.value = "";
      renderAuth(auth);
      if (auth.verification && auth.verification.authenticated) {
        showToast(auth.verification.message || "Session verified!");
        setAuthPanelExpanded(false);
      } else {
        showToast(auth.verification?.message || "Cookies saved, but verification did not confirm login.", true);
      }
    } catch (err) {
      showToast(`Cookie import failed: ${err.message}`, true);
    } finally {
      els.saveCookiesBtn.disabled = false;
    }
  });

  els.verifyCookiesBtn.addEventListener("click", async () => {
    els.verifyCookiesBtn.disabled = true;
    try {
      const auth = await apiFetch("/api/auth/verify", { method: "POST" });
      renderAuth(auth);
      showToast(auth.verification?.message || "Verification completed.", !auth.verification?.authenticated);
    } catch (err) {
      showToast(`Verification failed: ${err.message}`, true);
    } finally {
      els.verifyCookiesBtn.disabled = false;
    }
  });

  els.clearCookiesBtn.addEventListener("click", async () => {
    try {
      const auth = await apiFetch("/api/auth/cookies", { method: "DELETE" });
      renderAuth(auth);
      setAuthPanelExpanded(true);
      showToast("Session cookies cleared.");
    } catch (err) {
      showToast(`Failed clearing session: ${err.message}`, true);
    }
  });

  els.syncLibraryBtn.addEventListener("click", async () => {
    els.syncLibraryBtn.disabled = true;
    els.syncLibraryBtn.textContent = "Syncing…";
    try {
      const data = await apiFetch("/api/library/sync", { method: "POST" });
      upsertBooksList(data.books || [], data.data_dir);
      showToast(`Library synced (${data.counts.total} audiobooks found).`);
      if (data.counts.available > 0 && data.counts.extracted === 0) {
        setActiveTab("all", true);
      }
    } catch (err) {
      showToast(`Library sync failed: ${err.message}`, true);
    } finally {
      els.syncLibraryBtn.disabled = false;
      els.syncLibraryBtn.textContent = "Sync Google Play Library";
    }
  });

  els.exportAllBtn.addEventListener("click", async () => {
    els.exportAllBtn.disabled = true;
    try {
      const res = await apiFetch("/api/books/export-all", { method: "POST" });
      if (res.queued_count > 0) {
        showToast(`Queued ${res.queued_count} audiobook(s) for export.`);
      } else {
        showToast("No unextracted audiobooks waiting to export.");
      }
    } catch (err) {
      showToast(`Export All failed: ${err.message}`, true);
    } finally {
      els.exportAllBtn.disabled = false;
    }
  });

  els.reconcileBtn.addEventListener("click", async () => {
    els.reconcileBtn.disabled = true;
    try {
      const data = await apiFetch("/api/books/reconcile", { method: "POST" });
      upsertBooksList(data.books || [], data.data_dir);
      showToast(`Data folder rescanned (${data.counts.extracted} extracted).`);
    } catch (err) {
      showToast(`Rescan failed: ${err.message}`, true);
    } finally {
      els.reconcileBtn.disabled = false;
    }
  });

  els.booksList.addEventListener("click", async (ev) => {
    const btn = ev.target.closest(".export-btn");
    if (!btn) return;
    const volumeId = btn.dataset.id;
    if (!volumeId) return;
    btn.disabled = true;
    try {
      const res = await apiFetch(`/api/books/${encodeURIComponent(volumeId)}/export`, {
        method: "POST",
      });
      if (res.book) upsertSingleBook(res.book);
    } catch (err) {
      showToast(`Export failed: ${err.message}`, true);
      btn.disabled = false;
    }
  });

  for (const tab of els.tabs) {
    tab.addEventListener("click", () => {
      setActiveTab(tab.dataset.filter, true);
    });
  }

  els.searchInput.addEventListener("input", () => {
    state.search = els.searchInput.value;
    renderLibrary();
  });

  loadInitialState();
  connectSSE();
})();
