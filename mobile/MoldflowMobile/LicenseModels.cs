using System.Text.Json.Serialization;

namespace MoldflowMobile;

// ============================================================================
// STAGE 5 NETWORK LICENSE DATA MODELS
// ============================================================================

public class LicenseServerSummary
{
    [JsonPropertyName("server_id")]
    public string ServerId { get; set; } = string.Empty;

    [JsonPropertyName("hostname")]
    public string Hostname { get; set; } = string.Empty;

    [JsonPropertyName("display_name")]
    public string DisplayName { get; set; } = string.Empty;

    [JsonPropertyName("status")]
    public string Status { get; set; } = "UNKNOWN";

    [JsonPropertyName("last_successful_poll")]
    public string? LastSuccessfulPoll { get; set; }

    [JsonPropertyName("last_poll_attempt")]
    public string? LastPollAttempt { get; set; }

    [JsonPropertyName("last_error_code")]
    public int? LastErrorCode { get; set; }

    [JsonPropertyName("last_error_message")]
    public string? LastErrorMessage { get; set; }

    [JsonPropertyName("data_state")]
    public string DataState { get; set; } = "AVAILABLE";

    [JsonIgnore]
    public bool IsOnline => Status.Equals("UP", StringComparison.OrdinalIgnoreCase);

    [JsonIgnore]
    public bool IsAvailable => DataState.Equals("AVAILABLE", StringComparison.OrdinalIgnoreCase);
}

public class LicenseComponentDetail
{
    [JsonPropertyName("feature_code")]
    public string FeatureCode { get; set; } = string.Empty;

    [JsonPropertyName("product_name")]
    public string? ProductName { get; set; }

    [JsonPropertyName("product_family")]
    public string? ProductFamily { get; set; }

    [JsonPropertyName("year_version")]
    public string? YearVersion { get; set; }

    [JsonPropertyName("catalog_status")]
    public string CatalogStatus { get; set; } = "UNKNOWN";

    [JsonPropertyName("in_use")]
    public int? InUse { get; set; }

    [JsonIgnore]
    public string DisplayVersion => string.IsNullOrWhiteSpace(YearVersion) ? "Version N/A" : $"v{YearVersion}";

    [JsonIgnore]
    public string DisplayTitle => !string.IsNullOrWhiteSpace(ProductName)
        ? ProductName
        : (!string.IsNullOrWhiteSpace(ProductFamily) ? $"{ProductFamily} ({FeatureCode})" : FeatureCode);
}

public class LicensePackageDetail
{
    [JsonPropertyName("feature_code")]
    public string FeatureCode { get; set; } = string.Empty;

    [JsonPropertyName("product_name")]
    public string? ProductName { get; set; }

    [JsonPropertyName("product_family")]
    public string? ProductFamily { get; set; }

    [JsonPropertyName("display_name")]
    public string DisplayName { get; set; } = string.Empty;

    [JsonPropertyName("total_issued")]
    public int? TotalIssued { get; set; }

    [JsonPropertyName("in_use")]
    public int? InUse { get; set; }

    [JsonPropertyName("available")]
    public int? Available { get; set; }

    [JsonPropertyName("utilization_pct")]
    public double? UtilizationPct { get; set; }

    [JsonPropertyName("status")]
    public string Status { get; set; } = "UNKNOWN";

    [JsonPropertyName("catalog_status")]
    public string CatalogStatus { get; set; } = "UNKNOWN";

    [JsonPropertyName("description")]
    public string? Description { get; set; }

    [JsonPropertyName("components")]
    public List<LicenseComponentDetail> Components { get; set; } = new();

    // =========================================================
    // UI Helpers
    // =========================================================

    [JsonIgnore]
    public string EffectiveDisplayName
    {
        get
        {
            if (!string.IsNullOrWhiteSpace(DisplayName))
                return DisplayName;
            if (!string.IsNullOrWhiteSpace(ProductName))
                return ProductName;
            if (string.Equals(ProductFamily, "MFAA", StringComparison.OrdinalIgnoreCase) ||
                string.Equals(CatalogStatus, "UNKNOWN", StringComparison.OrdinalIgnoreCase))
            {
                return $"Unknown Moldflow Product ({ProductFamily ?? FeatureCode})";
            }
            return !string.IsNullOrWhiteSpace(ProductFamily) ? ProductFamily : FeatureCode;
        }
    }

    [JsonIgnore]
    public bool IsDataAvailable => TotalIssued.HasValue && InUse.HasValue && Available.HasValue;

    [JsonIgnore]
    public string SeatsDisplay
    {
        get
        {
            if (!IsDataAvailable)
                return "Utilization unavailable";
            return $"{InUse} / {TotalIssued} in use";
        }
    }

    [JsonIgnore]
    public string AvailableDisplay
    {
        get
        {
            if (!IsDataAvailable)
                return "Unavailable";
            return $"{Available} free";
        }
    }

    [JsonIgnore]
    public double UtilizationRatio
    {
        get
        {
            if (!UtilizationPct.HasValue)
                return 0.0;
            return Math.Clamp(UtilizationPct.Value / 100.0, 0.0, 1.0);
        }
    }

    [JsonIgnore]
    public string StatusBadgeText
    {
        get
        {
            if (!IsDataAvailable)
                return "UNAVAILABLE";
            if (Status.Equals("EXHAUSTED", StringComparison.OrdinalIgnoreCase) || (Available.HasValue && Available.Value == 0 && TotalIssued > 0))
                return "EXHAUSTED";
            return "AVAILABLE";
        }
    }

    [JsonIgnore]
    public string StatusBadgeBg => StatusBadgeText switch
    {
        "AVAILABLE" => "#DCFCE7",
        "EXHAUSTED" => "#FEE2E2",
        _ => "#F1F5F9"
    };

    [JsonIgnore]
    public string StatusBadgeTextColor => StatusBadgeText switch
    {
        "AVAILABLE" => "#16A34A",
        "EXHAUSTED" => "#DC2626",
        _ => "#697386"
    };

    [JsonIgnore]
    public bool HasComponents => Components != null && Components.Count > 0;
}

public class LicenseServerDetail
{
    [JsonPropertyName("server_id")]
    public string ServerId { get; set; } = string.Empty;

    [JsonPropertyName("hostname")]
    public string Hostname { get; set; } = string.Empty;

    [JsonPropertyName("display_name")]
    public string DisplayName { get; set; } = string.Empty;

    [JsonPropertyName("lmgrd_port")]
    public int LmgrdPort { get; set; } = 27000;

    [JsonPropertyName("vendor_daemon")]
    public string VendorDaemon { get; set; } = "adskflex";

    [JsonPropertyName("vendor_daemon_port")]
    public int? VendorDaemonPort { get; set; }

    [JsonPropertyName("status")]
    public string Status { get; set; } = "UNKNOWN";

    [JsonPropertyName("last_successful_poll")]
    public string? LastSuccessfulPoll { get; set; }

    [JsonPropertyName("last_poll_attempt")]
    public string? LastPollAttempt { get; set; }

    [JsonPropertyName("last_error_code")]
    public int? LastErrorCode { get; set; }

    [JsonPropertyName("last_error_message")]
    public string? LastErrorMessage { get; set; }

    [JsonPropertyName("is_active")]
    public bool IsActive { get; set; } = true;

    [JsonPropertyName("data_state")]
    public string DataState { get; set; } = "AVAILABLE";

    [JsonPropertyName("packages")]
    public List<LicensePackageDetail> Packages { get; set; } = new();

    // =========================================================
    // UI Helpers
    // =========================================================

    [JsonIgnore]
    public string NormalizedStatus => (Status ?? "UNKNOWN").Trim().ToUpperInvariant();

    [JsonIgnore]
    public bool IsOnline => NormalizedStatus == "UP";

    [JsonIgnore]
    public bool IsDataAvailable => DataState.Equals("AVAILABLE", StringComparison.OrdinalIgnoreCase) && IsOnline;

    [JsonIgnore]
    public bool IsUnavailable => !IsDataAvailable;

    [JsonIgnore]
    public string StatusBadgeBg => NormalizedStatus switch
    {
        "UP" => "#DCFCE7",
        "DOWN" => "#FEE2E2",
        "VENDOR_DOWN" => "#FFEDD5",
        "STALE" => "#FEF3C7",
        _ => "#F1F5F9"
    };

    [JsonIgnore]
    public string StatusBadgeTextColor => NormalizedStatus switch
    {
        "UP" => "#16A34A",
        "DOWN" => "#DC2626",
        "VENDOR_DOWN" => "#C2410C",
        "STALE" => "#B45309",
        _ => "#697386"
    };

    [JsonIgnore]
    public string HealthDisplayText
    {
        get
        {
            return NormalizedStatus switch
            {
                "UP" => "Online",
                "DOWN" => "Server Down",
                "VENDOR_DOWN" => "Vendor Daemon Down",
                "STALE" => "Stale Data",
                _ => "Unknown State"
            };
        }
    }

    [JsonIgnore]
    public string FormattedLastPoll
    {
        get
        {
            if (string.IsNullOrWhiteSpace(LastSuccessfulPoll))
                return "Never polled";
            if (DateTimeOffset.TryParse(LastSuccessfulPoll, out var dt))
                return dt.LocalDateTime.ToString("dd MMM yyyy, hh:mm tt");
            return LastSuccessfulPoll;
        }
    }
}

public class ServerOverviewItem
{
    [JsonPropertyName("server_id")]
    public string ServerId { get; set; } = string.Empty;

    [JsonPropertyName("hostname")]
    public string Hostname { get; set; } = string.Empty;

    [JsonPropertyName("display_name")]
    public string DisplayName { get; set; } = string.Empty;

    [JsonPropertyName("status")]
    public string Status { get; set; } = "UNKNOWN";

    [JsonPropertyName("last_successful_poll")]
    public string? LastSuccessfulPoll { get; set; }

    [JsonPropertyName("last_poll_attempt")]
    public string? LastPollAttempt { get; set; }

    [JsonPropertyName("last_error_code")]
    public int? LastErrorCode { get; set; }

    [JsonPropertyName("last_error_message")]
    public string? LastErrorMessage { get; set; }

    [JsonPropertyName("data_state")]
    public string DataState { get; set; } = "AVAILABLE";

    [JsonPropertyName("products")]
    public List<LicensePackageDetail> Products { get; set; } = new();

    // =========================================================
    // UI Helpers
    // =========================================================

    [JsonIgnore]
    public string NormalizedStatus => (Status ?? "UNKNOWN").Trim().ToUpperInvariant();

    [JsonIgnore]
    public bool IsOnline => NormalizedStatus == "UP";

    [JsonIgnore]
    public bool IsDataAvailable => DataState.Equals("AVAILABLE", StringComparison.OrdinalIgnoreCase) && IsOnline;

    [JsonIgnore]
    public bool IsUnavailable => !IsDataAvailable;

    [JsonIgnore]
    public string StatusBadgeBg => NormalizedStatus switch
    {
        "UP" => "#DCFCE7",
        "DOWN" => "#FEE2E2",
        "VENDOR_DOWN" => "#FFEDD5",
        "STALE" => "#FEF3C7",
        _ => "#F1F5F9"
    };

    [JsonIgnore]
    public string StatusBadgeTextColor => NormalizedStatus switch
    {
        "UP" => "#16A34A",
        "DOWN" => "#DC2626",
        "VENDOR_DOWN" => "#C2410C",
        "STALE" => "#B45309",
        _ => "#697386"
    };

    [JsonIgnore]
    public string HealthDisplayText
    {
        get
        {
            return NormalizedStatus switch
            {
                "UP" => "ONLINE",
                "DOWN" => "SERVER DOWN",
                "VENDOR_DOWN" => "VENDOR DOWN",
                "STALE" => "STALE",
                _ => "UNKNOWN"
            };
        }
    }

    [JsonIgnore]
    public string FormattedLastPoll
    {
        get
        {
            if (string.IsNullOrWhiteSpace(LastSuccessfulPoll))
                return "Never polled";
            if (DateTimeOffset.TryParse(LastSuccessfulPoll, out var dt))
                return dt.LocalDateTime.ToString("hh:mm tt");
            return LastSuccessfulPoll;
        }
    }

    [JsonIgnore]
    public string ServerSubtitle
    {
        get
        {
            if (IsDataAvailable)
                return $"Host: {Hostname} · Last updated {FormattedLastPoll}";

            return NormalizedStatus switch
            {
                "DOWN" => $"License data unavailable (Server unreachable)",
                "VENDOR_DOWN" => $"License data unavailable (adskflex vendor service offline)",
                "STALE" => $"License data unavailable (Stale data · Last: {FormattedLastPoll})",
                _ => $"License data unavailable (Status unknown)"
            };
        }
    }
}

public class AggregateProductInventory
{
    [JsonPropertyName("feature_code")]
    public string FeatureCode { get; set; } = string.Empty;

    [JsonPropertyName("product_name")]
    public string? ProductName { get; set; }

    [JsonPropertyName("product_family")]
    public string? ProductFamily { get; set; }

    [JsonPropertyName("display_name")]
    public string DisplayName { get; set; } = string.Empty;

    [JsonPropertyName("total_inventory")]
    public int TotalInventory { get; set; }

    [JsonPropertyName("current_inventory_used")]
    public int CurrentInventoryUsed { get; set; }

    [JsonPropertyName("inventory_available")]
    public int InventoryAvailable { get; set; }

    [JsonPropertyName("server_count")]
    public int ServerCount { get; set; }

    [JsonIgnore]
    public string EffectiveDisplayName
    {
        get
        {
            if (!string.IsNullOrWhiteSpace(DisplayName))
                return DisplayName;
            if (!string.IsNullOrWhiteSpace(ProductName))
                return ProductName;
            if (string.Equals(ProductFamily, "MFAA", StringComparison.OrdinalIgnoreCase))
                return "Unknown Moldflow Product (MFAA)";
            return !string.IsNullOrWhiteSpace(ProductFamily) ? ProductFamily : FeatureCode;
        }
    }

    [JsonIgnore]
    public string UsedSummaryText => $"{CurrentInventoryUsed} observed in use";

    [JsonIgnore]
    public string TotalSummaryText => $"{TotalInventory} total inventory across {ServerCount} server{(ServerCount == 1 ? "" : "s")}";

    [JsonIgnore]
    public string AvailableSummaryText => $"{InventoryAvailable} available";

    [JsonIgnore]
    public double UtilizationRatio => TotalInventory > 0
        ? Math.Clamp((double)CurrentInventoryUsed / TotalInventory, 0.0, 1.0)
        : 0.0;
}

public class EnvironmentInventory
{
    [JsonPropertyName("notice")]
    public string Notice { get; set; } = "Aggregate inventory across independent license servers. Licenses are not pooled.";

    [JsonPropertyName("is_pooled")]
    public bool IsPooled { get; set; } = false;

    [JsonPropertyName("total_servers")]
    public int TotalServers { get; set; }

    [JsonPropertyName("servers_up")]
    public int ServersUp { get; set; }

    [JsonPropertyName("servers_down")]
    public int ServersDown { get; set; }

    [JsonPropertyName("active_checkouts_total")]
    public int ActiveCheckoutsTotal { get; set; }

    [JsonPropertyName("products")]
    public List<AggregateProductInventory> Products { get; set; } = new();
}

public class LicenseOverviewResponse
{
    [JsonPropertyName("servers")]
    public List<ServerOverviewItem> Servers { get; set; } = new();

    [JsonPropertyName("environment_inventory")]
    public EnvironmentInventory EnvironmentInventory { get; set; } = new();
}

public class ActiveConsumerItem
{
    [JsonPropertyName("checkout_id")]
    public string CheckoutId { get; set; } = string.Empty;

    [JsonPropertyName("server_id")]
    public string ServerId { get; set; } = string.Empty;

    [JsonPropertyName("server_hostname")]
    public string? ServerHostname { get; set; }

    [JsonPropertyName("username")]
    public string Username { get; set; } = string.Empty;

    [JsonPropertyName("machine_name")]
    public string MachineName { get; set; } = string.Empty;

    [JsonPropertyName("product_name")]
    public string? ProductName { get; set; }

    [JsonPropertyName("package_feature")]
    public string PackageFeature { get; set; } = string.Empty;

    [JsonPropertyName("component_feature")]
    public string? ComponentFeature { get; set; }

    [JsonPropertyName("version")]
    public string? Version { get; set; }

    [JsonPropertyName("checkout_time")]
    public string? CheckoutTime { get; set; }

    [JsonPropertyName("checkout_time_precision")]
    public string? CheckoutTimePrecision { get; set; }

    [JsonPropertyName("is_borrowed")]
    public bool IsBorrowed { get; set; } = false;

    [JsonPropertyName("first_seen_at")]
    public string? FirstSeenAt { get; set; }

    [JsonPropertyName("last_seen_at")]
    public string? LastSeenAt { get; set; }

    // =========================================================
    // UI Helpers (Never exposes internal handles or PIDs)
    // =========================================================

    [JsonIgnore]
    public string DisplayProductTitle => !string.IsNullOrWhiteSpace(ProductName)
        ? ProductName
        : PackageFeature;

    [JsonIgnore]
    public string DisplayVersion => !string.IsNullOrWhiteSpace(Version) ? $"Version: {Version}" : "Version: Active";

    [JsonIgnore]
    public string BorrowedBadgeText => IsBorrowed ? "BORROWED" : "NETWORK";

    [JsonIgnore]
    public string CheckoutTimeDisplay => !string.IsNullOrWhiteSpace(CheckoutTime) ? CheckoutTime : "Recent";
}

public class LicenseEventItem
{
    [JsonPropertyName("event_id")]
    public string EventId { get; set; } = string.Empty;

    [JsonPropertyName("server_id")]
    public string ServerId { get; set; } = string.Empty;

    [JsonPropertyName("server_hostname")]
    public string? ServerHostname { get; set; }

    [JsonPropertyName("event_type")]
    public string EventType { get; set; } = string.Empty;

    [JsonPropertyName("feature_code")]
    public string? FeatureCode { get; set; }

    [JsonPropertyName("checkout_id")]
    public string? CheckoutId { get; set; }

    [JsonPropertyName("username")]
    public string? Username { get; set; }

    [JsonPropertyName("machine_name")]
    public string? MachineName { get; set; }

    [JsonPropertyName("selected_component_code")]
    public string? SelectedComponentCode { get; set; }

    [JsonPropertyName("previous_in_use")]
    public int? PreviousInUse { get; set; }

    [JsonPropertyName("new_in_use")]
    public int? NewInUse { get; set; }

    [JsonPropertyName("total_issued")]
    public int? TotalIssued { get; set; }

    [JsonPropertyName("detected_at")]
    public string DetectedAt { get; set; } = string.Empty;

    [JsonPropertyName("details")]
    public object? Details { get; set; }

    // =========================================================
    // UI Helpers
    // =========================================================

    [JsonIgnore]
    public string FriendlyTitle
    {
        get
        {
            return (EventType ?? string.Empty).Trim().ToUpperInvariant() switch
            {
                "CHECKOUT" => "License Checked Out",
                "RETURN" => "License Returned",
                "EXHAUSTED" => "Licenses Exhausted",
                "AVAILABLE" => "Licenses Available Again",
                "SERVER_UP" => "Server Came Online",
                "SERVER_DOWN" => "Server Went Offline",
                "VENDOR_DOWN" => "Vendor Service Down",
                _ => $"Event: {EventType}"
            };
        }
    }

    [JsonIgnore]
    public string Subtitle
    {
        get
        {
            var parts = new List<string>();
            if (!string.IsNullOrWhiteSpace(Username))
                parts.Add($"User: {Username}");
            if (!string.IsNullOrWhiteSpace(MachineName))
                parts.Add($"Host: {MachineName}");
            if (!string.IsNullOrWhiteSpace(FeatureCode))
                parts.Add($"Feature: {FeatureCode}");
            if (!string.IsNullOrWhiteSpace(ServerHostname))
                parts.Add($"Server: {ServerHostname}");

            if (NewInUse.HasValue && TotalIssued.HasValue)
                parts.Add($"In use: {NewInUse}/{TotalIssued}");

            return parts.Count > 0 ? string.Join(" · ", parts) : "No details";
        }
    }

    [JsonIgnore]
    public string FormattedTime
    {
        get
        {
            if (string.IsNullOrWhiteSpace(DetectedAt))
                return string.Empty;
            if (DateTimeOffset.TryParse(DetectedAt, out var dt))
                return dt.LocalDateTime.ToString("dd MMM yyyy, hh:mm tt");
            return DetectedAt;
        }
    }
}

public class LicenseHistoryResponse
{
    [JsonPropertyName("items")]
    public List<LicenseEventItem> Items { get; set; } = new();

    [JsonPropertyName("total")]
    public int Total { get; set; }

    [JsonPropertyName("limit")]
    public int Limit { get; set; } = 50;

    [JsonPropertyName("offset")]
    public int Offset { get; set; } = 0;
}
