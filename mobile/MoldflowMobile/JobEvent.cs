using System.Text.Json.Serialization;

namespace MoldflowMobile;

public class JobEvent
{
    [JsonPropertyName("id")]
    public int Id { get; set; }

    [JsonPropertyName("job_id")]
    public string JobId { get; set; } = string.Empty;

    [JsonPropertyName("status")]
    public string Status { get; set; } = string.Empty;

    [JsonPropertyName("percent")]
    public double? Percent { get; set; }

    [JsonPropertyName("finished")]
    public bool Finished { get; set; }

    [JsonPropertyName("error_message")]
    public string? ErrorMessage { get; set; }

    [JsonPropertyName("received_at")]
    public DateTimeOffset? ReceivedAt { get; set; }
}