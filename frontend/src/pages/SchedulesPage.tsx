import { useCallback, useEffect, useState } from "react";
import { getApiClient } from "../host";
import { formatIso } from "../ui/format";
import { BackendUnavailable, Badge, Menu, PageHeader } from "../ui/components";
import type { WorkflowsApi } from "../api/client";
import type { ScheduleListItem } from "../api/types";
import { ActionDialog } from "../ui/components/ActionDialog";

export interface SchedulesPageProps {
  /** Injected for tests; defaults to the host-bound client. */
  client?: WorkflowsApi;
  onOpenWorkflows?: () => void;
}

type LoadState =
  | { kind: "loading" }
  | { kind: "error"; detail?: string }
  | { kind: "ready"; items: ScheduleListItem[] };

export function SchedulesPage({ client, onOpenWorkflows }: SchedulesPageProps): React.ReactElement {
  const api = client ?? getApiClient();
  const [state, setState] = useState<LoadState>({ kind: "loading" });
  const [message, setMessage] = useState<string | null>(null);
  const [reloadKey, setReloadKey] = useState(0);
  const [action, setAction] = useState<{ kind: "edit" | "delete"; schedule: ScheduleListItem } | null>(null);

  const reload = useCallback(() => setReloadKey((k) => k + 1), []);

  useEffect(() => {
    let active = true;
    api
      .listSchedules()
      .then((items) => {
        if (active) setState({ kind: "ready", items });
      })
      .catch((err: unknown) => {
        if (active) {
          setState({ kind: "error", detail: err instanceof Error ? err.message : String(err) });
        }
      });
    return () => {
      active = false;
    };
  }, [api, reloadKey]);

  // Run a client action, report it, and refresh the list on success.
  const act = useCallback(
    (verb: string, id: string, call: () => Promise<unknown>) => {
      setMessage(`${verb} ${id}…`);
      call()
        .then(() => {
          setMessage(`${verb} ${id} ✓`);
          reload();
        })
        .catch((err: unknown) =>
          setMessage(err instanceof Error ? err.message : `Failed: ${verb} ${id}`),
        );
    },
    [reload],
  );

  const handleEdit = useCallback(
    (s: ScheduleListItem) => {
      setAction({ kind: "edit", schedule: s });
    },
    [act, api],
  );

  const handleDelete = useCallback(
    (s: ScheduleListItem) => {
      setAction({ kind: "delete", schedule: s });
    },
    [act, api],
  );

  if (state.kind === "loading") {
    return <p className="hw-page">Loading schedules…</p>;
  }
  if (state.kind === "error") {
    return <BackendUnavailable resource="schedules" detail={state.detail} />;
  }

  return (
    <div className="hw-page">
      {action && <ActionDialog
        title={action.kind === "edit" ? "Edit schedule" : "Delete schedule"}
        description={action.kind === "edit" ? `Cron schedule for ${action.schedule.workflow_id} (${action.schedule.timezone}).` : `Delete the schedule for "${action.schedule.workflow_id}"? The workflow and run history will remain.`}
        submitLabel={action.kind === "edit" ? "Save schedule" : "Delete schedule"}
        danger={action.kind === "delete"}
        {...(action.kind === "edit" ? {
          fieldLabel: "Cron expression", initialValue: action.schedule.cron_expression ?? "",
          validate: (text: string) => text.split(/\s+/).length === 5 ? null : "Enter a five-field cron expression.",
        } : {})}
        onSubmit={(text) => action.kind === "edit" ? api.editSchedule(action.schedule.hermes_cron_id, text) : api.deleteSchedule(action.schedule.hermes_cron_id)}
        onClose={() => setAction(null)}
        onSuccess={() => { setMessage(`${action.kind === "edit" ? "Edited" : "Deleted"} ${action.schedule.hermes_cron_id}`); setAction(null); reload(); }}
      />}
      <PageHeader title="Schedules" />
      <p className="hw-note">
        A schedule comes from a workflow with a <strong>cron</strong> trigger. To add one, open a
        workflow in{" "}
        <a className="hw-link" href="#workflows" onClick={(event) => { if (onOpenWorkflows) { event.preventDefault(); onOpenWorkflows(); } }}>
          Workflows
        </a>{" "}
        and set its trigger to cron. Below you can pause, resume, run now, edit the cron expression,
        or delete an existing schedule. These are multi-node <strong>Workflow</strong> schedules;
        Hermes Automation Blueprints (single-prompt automations) are managed separately and also
        appear on the host Schedules surface.
      </p>
      {message !== null && (
        <p role="status" className="hw-status">
          {message}
        </p>
      )}
      {state.items.length === 0 ? (
        <p>No schedules yet. Deploy a cron-triggered workflow to create one.</p>
      ) : (
        <table className="hw-table hw-table--nowrap">
          <thead>
            <tr>
              <th>Workflow</th>
              <th>Cron expression</th>
              <th>Timezone</th>
              <th>Enabled</th>
              <th>Last run</th>
              <th>Next run</th>
              <th>Hermes Cron ID</th>
              <th>Actions</th>
            </tr>
          </thead>
          <tbody>
            {state.items.map((s) => (
              <tr key={s.hermes_cron_id}>
                <td>
                  {s.workflow_id} <Badge tone="kind">Workflow</Badge>
                </td>
                <td>
                  <code>{s.cron_expression ?? "—"}</code>
                </td>
                <td>{s.timezone}</td>
                <td>
                  <Badge tone={s.enabled ? "completed" : "cancelled"}>
                    {s.enabled ? "on" : "paused"}
                  </Badge>
                </td>
                <td>{formatIso(s.last_run)}</td>
                <td>{formatIso(s.next_run)}</td>
                <td>
                  <code>{s.hermes_cron_id}</code>
                </td>
                <td>
                  <Menu
                    size="sm"
                    align="end"
                    label="Actions"
                    items={[
                      {
                        key: "pause",
                        label: "Pause",
                        onSelect: () =>
                          act("Pause", s.hermes_cron_id, () => api.pauseSchedule(s.hermes_cron_id)),
                      },
                      {
                        key: "resume",
                        label: "Resume",
                        onSelect: () =>
                          act("Resume", s.hermes_cron_id, () => api.resumeSchedule(s.hermes_cron_id)),
                      },
                      {
                        key: "run",
                        label: "Run now",
                        onSelect: () =>
                          act("Run", s.hermes_cron_id, () => api.runScheduleNow(s.hermes_cron_id)),
                      },
                      { key: "edit", label: "Edit", onSelect: () => handleEdit(s) },
                      { key: "delete", label: "Delete", onSelect: () => handleDelete(s) },
                    ]}
                  />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
