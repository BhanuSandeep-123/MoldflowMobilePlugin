using System.Text.Json.Serialization;

namespace MoldflowMobile;

public class Job
{
    // =========================================================
    // Backend job fields
    // =========================================================

    [JsonPropertyName("job_id")]
    public string JobId { get; set; } = string.Empty;

    [JsonPropertyName("name")]
    public string Name { get; set; } = string.Empty;

    [JsonPropertyName("job_type")]
    public string JobType { get; set; } = string.Empty;

    [JsonPropertyName("status")]
    public string Status { get; set; } = string.Empty;

    [JsonPropertyName("percent")]
    public double Percent { get; set; }

    // Backend returns Unix seconds in "started".
    [JsonPropertyName("started")]
    public double? Started { get; set; }

    // Backend returns Boolean in "finished".
    [JsonPropertyName("finished")]
    public bool Finished { get; set; }

    [JsonPropertyName("error_message")]
    public string? ErrorMessage { get; set; }

    [JsonPropertyName("user_id")]
    public string UserId { get; set; } = string.Empty;

    [JsonPropertyName("machine_id")]
    public string MachineId { get; set; } = string.Empty;

    [JsonPropertyName("created_at")]
    public DateTimeOffset? CreatedAt { get; set; }

    [JsonPropertyName("updated_at")]
    public DateTimeOffset? UpdatedAt { get; set; }

    [JsonPropertyName("update_count")]
    public int UpdateCount { get; set; }

    // =========================================================
    // Simulation Compute Manager fields
    // =========================================================

    [JsonPropertyName("scm_job_id")]
    public string? ScmJobId { get; set; }

    [JsonPropertyName("scm_type")]
    public string? ScmType { get; set; }

    [JsonPropertyName("compute_source")]
    public string? ComputeSource { get; set; }

    [JsonPropertyName("scm_user")]
    public string? ScmUser { get; set; }

    [JsonPropertyName("worker")]
    public string? Worker { get; set; }

    [JsonPropertyName("parent_job_id")]
    public string? ParentJobId { get; set; }

    // =========================================================
    // Convenience properties for UI
    // =========================================================

    [JsonIgnore]
    public double ProgressValue =>
        Math.Clamp(Percent / 100.0, 0.0, 1.0);

    [JsonIgnore]
    public string DisplayStatus =>
        string.IsNullOrWhiteSpace(Status)
            ? "UNKNOWN"
            : Status.Trim().ToUpperInvariant();

    [JsonIgnore]
    public bool IsRunning =>
        DisplayStatus is "RUNNING" or "INPROGRESS";

    [JsonIgnore]
    public bool IsCompleted =>
        DisplayStatus == "COMPLETED";

    [JsonIgnore]
    public bool IsFailed =>
        DisplayStatus is
            "FAILED" or
            "CANCELED" or
            "CANCELLED" or
            "TIMEDOUT";

    // Distinct from IsFailed (which lumps FAILED/CANCELED/TIMEDOUT together
    // for "can this be removed from the list") — the dashboard needs a
    // Canceled-only bucket that a real FAILED job never counts toward.
    [JsonIgnore]
    public bool IsCanceled =>
        DisplayStatus is "CANCELED" or "CANCELLED";

    [JsonIgnore]
    public bool IsQueued =>
        DisplayStatus is
            "QUEUED" or
            "CREATED" or
            "PENDING";

    // A job can be removed from the mobile list once it has reached any
    // terminal state — matches the backend's archive-eligibility check.
    [JsonIgnore]
    public bool IsRemovable =>
        IsCompleted || IsFailed;

    [JsonIgnore]
    public string StartedDisplay
    {
        get
        {
            if (!Started.HasValue || Started.Value <= 0)
                return "Not started";

            try
            {
                return DateTimeOffset
                    .FromUnixTimeSeconds((long)Started.Value)
                    .LocalDateTime
                    .ToString("dd MMM yyyy, hh:mm tt");
            }
            catch
            {
                return "Not started";
            }
        }
    }

    // The backend has no separate "finished_at" column -- Finished only
    // ever flips to true on the same report that sets the job's final
    // status, so UpdatedAt at that point IS the finish time.
    [JsonIgnore]
    public string FinishedDisplay
    {
        get
        {
            if (!Finished || !UpdatedAt.HasValue)
                return "Not finished yet";

            try
            {
                return UpdatedAt.Value
                    .LocalDateTime
                    .ToString("dd MMM yyyy, hh:mm tt");
            }
            catch
            {
                return "Not finished yet";
            }
        }
    }

    // =========================================================
    // SCM display helpers
    // =========================================================

    [JsonIgnore]
    public string ScmJobIdDisplay =>
        string.IsNullOrWhiteSpace(ScmJobId)
            ? "Not available"
            : ScmJobId;

    [JsonIgnore]
    public string ScmTypeDisplay =>
        string.IsNullOrWhiteSpace(ScmType)
            ? "Not available"
            : ScmType;

    [JsonIgnore]
    public string ComputeSourceDisplay =>
        string.IsNullOrWhiteSpace(ComputeSource)
            ? "Not available"
            : ComputeSource;

    [JsonIgnore]
    public string ScmUserDisplay =>
        string.IsNullOrWhiteSpace(ScmUser)
            ? "Not available"
            : ScmUser;

    [JsonIgnore]
    public string WorkerDisplay =>
        string.IsNullOrWhiteSpace(Worker)
            ? "Not available"
            : Worker;

    [JsonIgnore]
    public string ParentJobDisplay =>
        string.IsNullOrWhiteSpace(ParentJobId)
            ? "None"
            : ParentJobId;
}