import { getBasePath } from "../../host";

export interface BackendUnavailableProps {
  resource: string;
  detail?: string;
}

const DOCS_URL = "https://github.com/hermegeddon/hermes-workflows-windows/blob/development/hermes-workflows-py/README.md";

/** Shared by the web and Desktop adapters, both backed by the Python plugin. */
export function BackendUnavailable({ resource, detail }: BackendUnavailableProps): React.ReactElement {
  return (
    <div className="hw-page hw-backend-unavailable">
      <h2>Could not load {resource}</h2>
      <p>
        The Workflows view loaded, but the request for {resource} failed on the
        selected gateway and profile. Check the backend error below before changing setup.
      </p>
      <ol>
        <li>Check that the selected gateway is connected and the intended profile is selected.</li>
        <li>
          In <a href={`${getBasePath()}/plugins`}>Plugins</a>, check that the
          Windows-native Python <strong>hermes-workflows</strong> backend is installed
          and enabled for that gateway and profile. Desktop's Workflows switch enables
          the interface separately.
        </li>
        <li>
          If you just installed or enabled the backend, restart that profile's gateway
          when Hermes requests it, then reopen Workflows. Check the gateway logs if
          the error persists.
        </li>
      </ol>
      <p>
        See the <a href={DOCS_URL} target="_blank" rel="noreferrer">Python plugin setup guide</a>
        {" "}for installation and hosts that require a standalone server.
      </p>
      {detail ? <p className="hw-muted">Backend error: <code>{detail}</code></p> : null}
    </div>
  );
}
