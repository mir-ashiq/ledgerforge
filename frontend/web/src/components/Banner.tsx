/** Status banner (API state, posting outcome) + the screen-reader channel. */

export interface Notice {
  kind: 'ok' | 'err' | 'info';
  text: string;
}

export function Banner({ notice, loading, apiError, onRetry }: {
  notice: Notice | null;
  loading: boolean;
  apiError: boolean;
  onRetry: () => void;
}) {
  return (
    <div className="banner-slot">
      {apiError && (
        <div className="banner err" role="alert">
          <span>Cannot reach the engine — start it with <code>make dev</code> (API at /api/v1).</span>
          <button type="button" onClick={onRetry}>Retry</button>
        </div>
      )}
      {!apiError && loading && (
        <div className="banner info" role="status">
          Loading chart of accounts…
        </div>
      )}
      {notice && !apiError && !loading && (
        <div className={`banner ${notice.kind}`} role="status">
          {notice.text}
        </div>
      )}
    </div>
  );
}
