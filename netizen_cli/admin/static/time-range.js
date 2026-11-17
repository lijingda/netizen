"use strict";

window.NetizenTimeRange = (() => {
  let dependencies;
  const timeRangeControllers = new WeakMap();
  const timeRangeTemplate = document.querySelector("#time-range-template");
  const timeRangeMobile = window.matchMedia("(max-width: 650px)");
  const timeRangeZone = Intl.DateTimeFormat().resolvedOptions().timeZone || "浏览器本地时区";
  let openTimeRangeFilter = null;
  let timeRangeModalOwner = null;
  let timeRangeModalIsolation = [];

  const timeRangePresetLabels = {
    all: "不限时间",
    today: "今天",
    yesterday: "昨天",
    "last-24-hours": "最近 24 小时",
    "last-7-days": "最近 7 天",
    "last-30-days": "最近 30 天",
    "last-3-months": "最近 3 个月",
  };

  function twoDigits(value) {
    return String(value).padStart(2, "0");
  }

  function localMinuteValue(value) {
    return [
      String(value.getFullYear()).padStart(4, "0"),
      "-",
      twoDigits(value.getMonth() + 1),
      "-",
      twoDigits(value.getDate()),
      "T",
      twoDigits(value.getHours()),
      ":",
      twoDigits(value.getMinutes()),
    ].join("");
  }

  function formatLocalMinute(value) {
    return localMinuteValue(value).replace("T", " ");
  }

  function utcOffsetLabel(value) {
    const total = -value.getTimezoneOffset();
    const sign = total >= 0 ? "+" : "-";
    const absolute = Math.abs(total);
    return `UTC${sign}${twoDigits(Math.floor(absolute / 60))}:${twoDigits(absolute % 60)}`;
  }

  function canonicalUtc(value) {
    return `${value.toISOString().slice(0, -1)}000+00:00`;
  }

  function sameLocalMinute(value, parts) {
    return value.getFullYear() === parts.year
      && value.getMonth() === parts.month - 1
      && value.getDate() === parts.day
      && value.getHours() === parts.hour
      && value.getMinutes() === parts.minute;
  }

  function parseLocalMinute(raw) {
    const match = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})$/.exec(raw);
    if (!match) return { date: null, error: "请选择有效的本地日期和时间。" };
    const parts = {
      year: Number(match[1]),
      month: Number(match[2]),
      day: Number(match[3]),
      hour: Number(match[4]),
      minute: Number(match[5]),
    };
    const value = new Date(0);
    value.setFullYear(parts.year, parts.month - 1, parts.day);
    value.setHours(parts.hour, parts.minute, 0, 0);
    if (!sameLocalMinute(value, parts)) {
      return { date: null, error: "该本地时间不存在，可能处于夏令时切换。" };
    }

    const currentOffset = value.getTimezoneOffset();
    const nearbyOffsets = new Set([
      new Date(value.getTime() - 24 * 60 * 60 * 1000).getTimezoneOffset(),
      new Date(value.getTime() + 24 * 60 * 60 * 1000).getTimezoneOffset(),
    ]);
    for (const candidateOffset of nearbyOffsets) {
      if (candidateOffset === currentOffset) continue;
      const alternative = new Date(
        value.getTime() + (candidateOffset - currentOffset) * 60 * 1000,
      );
      if (sameLocalMinute(alternative, parts)) {
        return { date: null, error: "该本地时间因夏令时切换而重复，请选择其他时间。" };
      }
    }
    return { date: value, error: "" };
  }

  function zoneDescription(values = []) {
    const dates = values.length ? values : [new Date()];
    const offsets = [...new Set(dates.map(utcOffsetLabel))];
    return `时区：${timeRangeZone} (${offsets.join(" → ")})`;
  }

  function setTimeRangeModalIsolation(controller, popover, active) {
    if (!active && timeRangeModalOwner !== controller) return;
    for (const node of timeRangeModalIsolation) node.removeAttribute("inert");
    timeRangeModalIsolation = [];
    timeRangeModalOwner = active ? controller : null;
    if (active) {
      let branch = popover;
      for (let parent = popover.parentElement; parent; parent = parent.parentElement) {
        for (const sibling of parent.children) {
          if (sibling === branch || sibling.matches("[data-time-range-backdrop]")) {
            continue;
          }
          if (!sibling.hasAttribute("inert")) {
            sibling.setAttribute("inert", "");
            timeRangeModalIsolation.push(sibling);
          }
        }
        branch = parent;
      }
    }
    document.body.classList.toggle(
      "time-range-modal-open",
      timeRangeModalOwner !== null,
    );
  }

  function allTimeRange() {
    return {
      kind: "all",
      from: "",
      before: "",
      startValue: "",
      endValue: "",
      summary: "全部时间",
    };
  }

  function timeRangeSummary(from, before, kind = "custom", now = new Date()) {
    if (!from && !before) return "全部时间";
    const rolling = kind.startsWith("last-");
    const start = new Date(from);
    // Show the last included minute, including a partially included rolling minute.
    const end = before ? new Date(new Date(before).getTime() - 1) : now;
    const startLabel = Number.isNaN(start.getTime()) ? from || "不限起点" : formatLocalMinute(start);
    const endLabel = !before && !rolling ? "不限终点"
      : Number.isNaN(end.getTime()) ? before : formatLocalMinute(end);
    const label = timeRangePresetLabels[kind];
    const name = label && kind !== "all" ? `${label}${!before && rolling ? "（至今）" : ""}` : "";
    const dates = [from ? start : null, before || rolling ? end : null]
      .filter((value) => value && !Number.isNaN(value.getTime()));
    const offsets = [...new Set(dates.map(utcOffsetLabel))].join(" → ");
    const heading = [name, offsets].filter(Boolean).join(" · ");
    return `${heading ? `${heading}\n` : ""}${startLabel} ~ ${endLabel}`;
  }

  function compactTimeRange(from, before, kind = "custom") {
    if (!from && !before) return "全部时间";
    if (kind !== "custom" && timeRangePresetLabels[kind]) return timeRangePresetLabels[kind];
    const date = (value, end = false) => {
      if (!value) return end ? "不限终点" : "不限起点";
      const parsed = new Date(new Date(value).getTime() - (end ? 1 : 0));
      return Number.isNaN(parsed.getTime()) ? value : localMinuteValue(parsed).slice(0, 10);
    };
    return `${date(from)} 至 ${date(before, true)}`;
  }

  function presetTimeRange(kind, now = new Date(), openEnded = false) {
    if (kind === "all") return allTimeRange();
    let from;
    let before;
    if (kind === "today") {
      from = new Date(now.getFullYear(), now.getMonth(), now.getDate());
      before = new Date(now.getFullYear(), now.getMonth(), now.getDate() + 1);
    } else if (kind === "yesterday") {
      from = new Date(now.getFullYear(), now.getMonth(), now.getDate() - 1);
      before = new Date(now.getFullYear(), now.getMonth(), now.getDate());
    } else if (kind === "last-3-months") {
      before = new Date(now.getTime());
      from = new Date(now.getTime());
      const day = from.getDate();
      from.setDate(1);
      from.setMonth(from.getMonth() - 3);
      const lastDay = new Date(from.getFullYear(), from.getMonth() + 1, 0).getDate();
      from.setDate(Math.min(day, lastDay));
    } else {
      const durationDays = {
        "last-24-hours": 1,
        "last-7-days": 7,
        "last-30-days": 30,
      }[kind];
      if (!durationDays) throw new Error("未知时间范围预设");
      before = new Date(now.getTime());
      from = new Date(now.getTime() - durationDays * 24 * 60 * 60 * 1000);
    }
    const rolling = kind.startsWith("last-");
    const fromUtc = canonicalUtc(from);
    const beforeUtc = openEnded && rolling ? "" : canonicalUtc(before);
    return {
      kind,
      from: fromUtc,
      before: beforeUtc,
      startValue: localMinuteValue(from),
      endValue: localMinuteValue(new Date(before.getTime() - 1)),
      summary: timeRangeSummary(fromUtc, beforeUtc, kind, now),
    };
  }

  function customTimeRange(startValue, endValue) {
    if (!startValue || !endValue) {
      return {
        range: null,
        error: "请选择开始时间和结束时间。",
        invalid: startValue ? "end" : "start",
      };
    }
    const parsedStart = parseLocalMinute(startValue);
    if (!parsedStart.date) {
      return { range: null, error: `开始时间：${parsedStart.error}`, invalid: "start" };
    }
    const parsedEnd = parseLocalMinute(endValue);
    if (!parsedEnd.date) {
      return { range: null, error: `结束时间：${parsedEnd.error}`, invalid: "end" };
    }
    if (parsedEnd.date < parsedStart.date) {
      return { range: null, error: "结束时间不能早于开始时间。", invalid: "end" };
    }
    const before = new Date(parsedEnd.date.getTime() + 60 * 1000);
    return {
      range: {
        kind: "custom",
        from: canonicalUtc(parsedStart.date),
        before: canonicalUtc(before),
        startValue,
        endValue,
        summary: timeRangeSummary(canonicalUtc(parsedStart.date), canonicalUtc(before)),
      },
      error: "",
      invalid: null,
    };
  }

  function initializeTimeRangeFilter(root) {
    if (!(timeRangeTemplate instanceof HTMLTemplateElement)) {
      throw new Error("时间范围模板不可用");
    }
    root.append(timeRangeTemplate.content.cloneNode(true));
    const identity = root.dataset.timeRangeId;
    const field = root.dataset.timeRangeField || "created";
    const label = root.querySelector("legend").textContent;
    const defaultKind = root.dataset.timeRangeDefault || "all";
    // Rolling update filters include later updates when this applied page is refreshed.
    const preset = (kind) => presetTimeRange(kind, new Date(), field === "updated");
    const fromNode = root.querySelector("[data-time-range-from]");
    const beforeNode = root.querySelector("[data-time-range-before]");
    const trigger = root.querySelector("[data-time-range-trigger]");
    const summary = root.querySelector("[data-time-range-summary]");
    const quickClear = root.querySelector("[data-time-range-quick-clear]");
    const backdrop = root.querySelector("[data-time-range-backdrop]");
    const popover = root.querySelector("[data-time-range-popover]");
    const title = root.querySelector("[data-time-range-dialog-title]");
    const start = root.querySelector("[data-time-range-start]");
    const end = root.querySelector("[data-time-range-end]");
    const help = root.querySelector("[data-time-range-help]");
    const zone = root.querySelector("[data-time-range-zone]");
    const error = root.querySelector("[data-time-range-error]");
    const done = root.querySelector("[data-time-range-done]");
    const cancel = root.querySelector("[data-time-range-cancel]");
    const clear = root.querySelector("[data-time-range-clear]");
    const presetButtons = [...root.querySelectorAll("[data-time-range-preset]")];

    fromNode.name = `${field}From`;
    beforeNode.name = `${field}Before`;
    title.textContent = label;
    quickClear.setAttribute("aria-label", `清除${label}筛选`);
    quickClear.title = `清除${label}筛选`;
    popover.id = `${identity}-popover`;
    title.id = `${identity}-title`;
    help.id = `${identity}-help`;
    zone.id = `${identity}-zone`;
    error.id = `${identity}-error`;
    start.id = `${identity}-start`;
    end.id = `${identity}-end`;
    trigger.setAttribute("aria-controls", popover.id);
    popover.setAttribute("aria-labelledby", title.id);
    start.setAttribute("aria-describedby", `${help.id} ${zone.id} ${error.id}`);
    end.setAttribute("aria-describedby", `${help.id} ${zone.id} ${error.id}`);

    let applied = preset(defaultKind);
    let draft = { ...applied };

    function setInputValidity(invalid) {
      for (const [node, name] of [[start, "start"], [end, "end"]]) {
        if (invalid === name) node.setAttribute("aria-invalid", "true");
        else node.removeAttribute("aria-invalid");
      }
    }

    function renderApplied() {
      fromNode.value = applied.from;
      beforeNode.value = applied.before;
      applied.summary = timeRangeSummary(applied.from, applied.before, applied.kind);
      summary.textContent = compactTimeRange(applied.from, applied.before, applied.kind);
      const dates = [applied.from, applied.before || new Date()].filter(Boolean)
        .map((value) => new Date(value)).filter((value) => !Number.isNaN(value.getTime()));
      trigger.title = `${applied.summary}\n${zoneDescription(dates)}`;
      trigger.classList.toggle("has-value", applied.kind !== "all");
      trigger.setAttribute("aria-label", `${label}：${applied.summary}`);
      quickClear.hidden = applied.kind === "all";
    }

    function renderDraft() {
      start.value = draft.startValue;
      end.value = draft.endValue;
      error.textContent = "";
      setInputValidity(null);
      done.disabled = false;
      for (const button of presetButtons) {
        button.setAttribute(
          "aria-pressed",
          String(button.dataset.timeRangePreset === draft.kind),
        );
      }
      const parsedDates = [start.value, end.value]
        .map(parseLocalMinute)
        .filter((item) => item.date)
        .map((item) => item.date);
      zone.textContent = zoneDescription(parsedDates);
    }

    function validateDraft() {
      const result = customTimeRange(start.value, end.value);
      draft = result.range || {
        kind: "custom",
        from: "",
        before: "",
        startValue: start.value,
        endValue: end.value,
        summary: "自定义范围",
      };
      error.textContent = result.error;
      setInputValidity(result.invalid);
      done.disabled = !result.range;
      for (const button of presetButtons) button.setAttribute("aria-pressed", "false");
      const parsedDates = [start.value, end.value]
        .map(parseLocalMinute)
        .filter((item) => item.date)
        .map((item) => item.date);
      zone.textContent = zoneDescription(parsedDates);
      return Boolean(result.range);
    }

    function syncPresentationMode() {
      const mobile = timeRangeMobile.matches && !popover.hidden;
      backdrop.hidden = !mobile;
      if (mobile) {
        popover.setAttribute("aria-modal", "true");
        popover.style.removeProperty("left");
        popover.style.removeProperty("right");
      } else {
        popover.removeAttribute("aria-modal");
        if (!popover.hidden) {
          const margin = 24;
          const width = Math.min(620, window.innerWidth - margin * 2);
          const rootBounds = root.getBoundingClientRect();
          const viewportLeft = Math.min(
            Math.max(rootBounds.left, margin),
            window.innerWidth - margin - width,
          );
          popover.style.left = `${viewportLeft - rootBounds.left}px`;
          popover.style.right = "auto";
        }
      }
      setTimeRangeModalIsolation(controller, popover, mobile);
    }

    function closePopover({ restoreFocus = true } = {}) {
      popover.hidden = true;
      backdrop.hidden = true;
      trigger.setAttribute("aria-expanded", "false");
      popover.removeAttribute("aria-modal");
      setTimeRangeModalIsolation(controller, popover, false);
      if (openTimeRangeFilter === controller) openTimeRangeFilter = null;
      if (restoreFocus) trigger.focus();
    }

    function cancelDraft(options) {
      draft = { ...applied };
      closePopover(options);
    }

    function prepareQuery() {
      if (applied.kind === "all" || applied.kind === "custom" || applied.restored) return;
      applied = preset(applied.kind);
      renderApplied();
    }

    function openPopover() {
      dependencies.closeOtherFilters();
      if (openTimeRangeFilter && openTimeRangeFilter !== controller) {
        openTimeRangeFilter.cancel({ restoreFocus: false });
      }
      openTimeRangeFilter = controller;
      draft = { ...applied };
      renderDraft();
      popover.hidden = false;
      trigger.setAttribute("aria-expanded", "true");
      syncPresentationMode();
      const selected = presetButtons.find(
        (button) => button.dataset.timeRangePreset === draft.kind,
      );
      (selected || start).focus();
    }

    const controller = {
      cancel: cancelDraft,
      prepareQuery,
      refreshSummary: renderApplied,
      presetKind() { return applied.kind !== "all" && applied.kind !== "custom" ? applied.kind : null; },
      setBounds(from, before, kind = "custom") {
        if (!from && !before) { this.reset("all"); return; }
        const startDate = new Date(from);
        const beforeDate = new Date(before);
        const endDate = new Date(beforeDate.getTime() - 1);
        // URL bounds stay absolute; the preset name is presentation metadata only.
        applied = { kind: Object.hasOwn(timeRangePresetLabels, kind) && kind !== "all" ? kind : "custom", from, before, restored: true,
          startValue: Number.isNaN(startDate.getTime()) ? "" : localMinuteValue(startDate),
          endValue: Number.isNaN(endDate.getTime()) ? "" : localMinuteValue(endDate) };
        draft = { ...applied };
        closePopover({ restoreFocus: false });
        renderApplied();
      },
      reset(kind = defaultKind) {
        applied = preset(kind);
        draft = { ...applied };
        closePopover({ restoreFocus: false });
        renderApplied();
      },
    };

    trigger.addEventListener("click", () => {
      if (popover.hidden) openPopover();
      else cancelDraft();
    });
    quickClear.addEventListener("click", () => {
      applied = allTimeRange();
      draft = allTimeRange();
      renderApplied();
      if (!popover.hidden) renderDraft();
      trigger.focus();
    });
    for (const button of presetButtons) {
      button.addEventListener("click", () => {
        draft = preset(button.dataset.timeRangePreset);
        renderDraft();
      });
    }
    start.addEventListener("input", validateDraft);
    end.addEventListener("input", validateDraft);
    clear.addEventListener("click", () => {
      draft = allTimeRange();
      renderDraft();
    });
    cancel.addEventListener("click", () => cancelDraft());
    done.addEventListener("click", () => {
      // A restored URL may have only one bound. Confirming it unchanged must
      // preserve that valid range; edited custom input still requires both dates.
      const unchanged = start.value === applied.startValue && end.value === applied.endValue;
      if (draft.kind === "custom" && !unchanged && !validateDraft()) {
        (start.getAttribute("aria-invalid") === "true" ? start : end).focus();
        return;
      }
      applied = { ...draft };
      renderApplied();
      closePopover();
    });
    backdrop.addEventListener("click", () => cancelDraft());
    popover.addEventListener("keydown", (event) => {
      if (event.key === "Escape") {
        event.preventDefault();
        cancelDraft();
        return;
      }
      if (event.key === "Enter" && (event.target === start || event.target === end)) {
        event.preventDefault();
        if (!done.disabled) done.click();
        return;
      }
      if (event.key !== "Tab") return;
      const focusable = [...popover.querySelectorAll("button:not(:disabled), input:not(:disabled)")]
        .filter((node) => !node.hidden);
      if (!focusable.length) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    });
    document.addEventListener("pointerdown", (event) => {
      if (!popover.hidden && !root.contains(event.target)) {
        cancelDraft({ restoreFocus: false });
      }
    });
    timeRangeMobile.addEventListener("change", syncPresentationMode);
    window.addEventListener("resize", syncPresentationMode);
    renderApplied();
    zone.textContent = zoneDescription();
    return controller;
  }

  function init(options) {
    dependencies = options;
    for (const root of document.querySelectorAll("[data-time-range]")) {
      timeRangeControllers.set(root, initializeTimeRangeFilter(root));
    }
  }

  function cancelOpen() {
    openTimeRangeFilter?.cancel({ restoreFocus: false });
  }

  return { init, controllerFor: (root) => timeRangeControllers.get(root), cancelOpen,
    leave: cancelOpen, presetTimeRange, timeRangePresetLabels, compactTimeRange, timeRangeSummary };
})();
