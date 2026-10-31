/* Python dashboard additions around the maintained frontend variant. */
(function () {
  "use strict";
  const sdk = window.__HERMES_PLUGIN_SDK__;
  const registry = window.__HERMES_PLUGINS__;
  const entry = document.currentScript;
  const React = sdk.React;
  const h = React.createElement;
  const base = "/api/plugins/hermes-workflows";
  let Original = null;
  let loadError = null;
  const listeners = new Set();
  const notify = () => listeners.forEach((listener) => listener({ Original, loadError }));

  function inspectorId() {
    try {
      return window.location.hash.startsWith("#run/")
        ? decodeURIComponent(window.location.hash.slice(5)) : null;
    } catch { return null; }
  }

  function ReviewGate({ runId, node, onReviewed }) {
    const [note, setNote] = React.useState("");
    const [busy, setBusy] = React.useState(false);
    const [error, setError] = React.useState(null);
    const submitting = React.useRef(false);
    async function decide(decision) {
      if (submitting.current || inspectorId() !== runId) return;
      submitting.current = true;
      setBusy(true);
      setError(null);
      try {
        await sdk.fetchJSON(`${base}/runs/${encodeURIComponent(runId)}/review`, {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ node_id: node.node_id, decision, ...(note.trim() ? { note: note.trim() } : {}) }),
        });
        onReviewed(node.node_id);
      } catch (failure) {
        setError(failure instanceof Error ? failure.message : "Could not submit review.");
      } finally {
        submitting.current = false;
        setBusy(false);
      }
    }
    return h("section", { className: "hwp-review", "aria-label": `Review ${node.node_id}` },
      h("h2", null, "Human review required"),
      h("p", null, `Step: ${node.node_id}`),
      h("label", null, "Review note (optional)",
        h("textarea", { value: note, disabled: busy, rows: 2, onChange: (event) => setNote(event.target.value) })),
      h("div", { className: "hwp-review-actions" },
        ...[["approved", "Approve"], ["rejected", "Reject"], ["needs_changes", "Request changes"]]
          .filter(([value]) => !Array.isArray(node.review_options) || node.review_options.includes(value)).map(([value, label]) =>
          h("button", { key: value, type: "button", disabled: busy, onClick: () => decide(value) }, label))),
      busy && h("p", { role: "status" }, "Submitting review…"),
      error && h("p", { role: "alert" }, error));
  }

  function Reviews() {
    const [view, setView] = React.useState({ runId: null, nodes: [], error: null });
    React.useEffect(() => {
      let disposed = false;
      let timer;
      let previous = null;
      async function poll() {
        const runId = inspectorId();
        if (previous !== runId) {
          previous = runId;
          setView({ runId, nodes: [], error: null });
        }
        if (runId) {
          try {
            const run = await sdk.fetchJSON(`${base}/runs/${encodeURIComponent(runId)}`);
            if (!disposed && inspectorId() === runId) {
              const nodes = ["created", "running", "waiting"].includes(run.status)
                ? Object.values(run.nodes || {}).filter((node) => node.status === "waiting_for_review") : [];
              setView({ runId, nodes, error: null });
            }
          } catch (failure) {
            if (!disposed && inspectorId() === runId) setView({ runId, nodes: [], error: "Could not load human reviews. Refresh to try again." });
          }
        }
        if (!disposed) timer = window.setTimeout(poll, 1000);
      }
      poll();
      return () => { disposed = true; window.clearTimeout(timer); };
    }, []);
    // A navigation may precede the next poll; never leave an old run actionable.
    if (view.runId !== inspectorId()) return null;
    return h("div", { className: "hwp-reviews", "aria-live": "polite" },
      view.error && h("p", { role: "alert" }, view.error),
      ...view.nodes.map((node) => h(ReviewGate, {
        key: `${view.runId}/${node.node_id}`, runId: view.runId, node,
        onReviewed: (nodeId) => setView((current) => current.runId !== view.runId ? current : ({ ...current, nodes: current.nodes.filter((item) => item.node_id !== nodeId) })),
      })));
  }

  function Dashboard(props) {
    const [loaded, setLoaded] = React.useState(() => ({ Original, loadError }));
    React.useEffect(() => {
      listeners.add(setLoaded);
      setLoaded({ Original, loadError });
      return () => listeners.delete(setLoaded);
    }, []);
    if (loaded.loadError) return h("p", { role: "alert" }, loaded.loadError);
    if (!loaded.Original) return h("p", { role: "status" }, "Loading Workflows…");
    return h(React.Fragment, null, h(Reviews), h(loaded.Original, props));
  }

  const script = document.createElement("script");
  script.src = new URL("compat/index.js", entry.src).href;
  const profile = entry.getAttribute("data-hermes-profile");
  if (profile !== null) script.setAttribute("data-hermes-profile", profile);
  const register = registry.register;
  function capture(name, component) {
    if (name === "hermes-workflows" && document.currentScript === script) {
      Original = component;
      notify();
    } else register.call(registry, name, component);
  }
  registry.register = capture;
  register.call(registry, "hermes-workflows", Dashboard);
  const restore = () => { if (registry.register === capture) registry.register = register; };
  script.onload = () => {
    restore();
    if (!Original) { loadError = "Workflows did not register. Refresh to try again."; notify(); }
  };
  script.onerror = () => { restore(); loadError = "Could not load Workflows. Refresh to try again."; notify(); };
  document.body.appendChild(script);
})();
