using System.Net.Http.Headers;
using System.Text;
using System.Text.Json;

namespace MoldflowMobile;

public class ApiService
{
    private readonly HttpClient _httpClient;

    public const string DefaultBaseUrl = "https://moldflowplugin-mobile-app.onrender.com";
    private const string BaseUrlPrefKey = "moldflow_base_url";
    private const string AuthTokenKey = "moldflow_auth_token";

    // JWT stored in memory and synchronized with SecureStorage
    private string? _token;

    private readonly JsonSerializerOptions _jsonOptions =
        new()
        {
            PropertyNameCaseInsensitive = true
        };

    public static string GetBaseUrl()
    {
        return Preferences.Default.Get(BaseUrlPrefKey, DefaultBaseUrl);
    }

    public static void SetBaseUrl(string url)
    {
        if (!string.IsNullOrWhiteSpace(url))
        {
            Preferences.Default.Set(BaseUrlPrefKey, url.TrimEnd('/'));
        }
    }

    public ApiService()
    {
        _httpClient = new HttpClient
        {
            Timeout = TimeSpan.FromSeconds(15)
        };
    }

    private Uri GetEndpointUri(string relativePath)
    {
        var baseUrl = GetBaseUrl().TrimEnd('/') + "/";
        return new Uri(new Uri(baseUrl), relativePath.TrimStart('/'));
    }

    // ---------------------------------------------------------
    // LOGIN & SESSION MANAGEMENT
    // ---------------------------------------------------------

    public async Task<string> LoginAsync(
        string email,
        string password)
    {
        try
        {
            var loginData = new
            {
                email,
                password
            };

            var json = JsonSerializer.Serialize(loginData);

            using var content = new StringContent(
                json,
                Encoding.UTF8,
                "application/json");

            var response = await _httpClient.PostAsync(
                GetEndpointUri("/auth/login"),
                content);

            var responseBody =
                await response.Content.ReadAsStringAsync();

            if (!response.IsSuccessStatusCode)
            {
                throw new Exception(
                    GetFriendlyHttpError(
                        response.StatusCode,
                        responseBody,
                        "Login failed"));
            }

            using var document =
                JsonDocument.Parse(responseBody);

            if (!document.RootElement.TryGetProperty(
                    "access_token",
                    out var tokenElement))
            {
                throw new Exception(
                    "Login succeeded, but the server did not return an access token.");
            }

            var token = tokenElement.GetString();

            if (string.IsNullOrWhiteSpace(token))
            {
                throw new Exception(
                    "The server returned an empty access token.");
            }

            _token = token;

            try
            {
                await SecureStorage.Default.SetAsync(AuthTokenKey, token);
            }
            catch (Exception ex)
            {
                System.Diagnostics.Debug.WriteLine($"SecureStorage save failed: {ex.Message}");
            }

            return token;
        }
        catch (HttpRequestException)
        {
            throw new Exception(
                $"Unable to connect to the Moldflow backend at {GetBaseUrl()}.\n\n" +
                "Make sure the FastAPI server is running.");
        }
        catch (TaskCanceledException)
        {
            throw new Exception(
                "The request timed out while connecting to the Moldflow backend.");
        }
    }

    // ---------------------------------------------------------
    // TOKEN PERSISTENCE (SecureStorage)
    // ---------------------------------------------------------

    public void SetToken(string token)
    {
        _token = token;
    }

    public string? GetToken()
    {
        return _token;
    }

    public async Task<string?> GetSavedTokenAsync()
    {
        if (!string.IsNullOrWhiteSpace(_token))
            return _token;

        try
        {
            _token = await SecureStorage.Default.GetAsync(AuthTokenKey);
        }
        catch (Exception ex)
        {
            System.Diagnostics.Debug.WriteLine($"SecureStorage read failed: {ex.Message}");
        }

        return _token;
    }

    public void ClearToken()
    {
        _token = null;
        try
        {
            SecureStorage.Default.Remove(AuthTokenKey);
        }
        catch (Exception ex)
        {
            System.Diagnostics.Debug.WriteLine($"SecureStorage clear failed: {ex.Message}");
        }
    }

    public async Task<bool> ValidateSessionAsync(string? token = null)
    {
        var tokenToValidate = token ?? _token ?? await GetSavedTokenAsync();

        if (string.IsNullOrWhiteSpace(tokenToValidate))
            return false;

        try
        {
            using var request = new HttpRequestMessage(HttpMethod.Get, GetEndpointUri("/me"));
            request.Headers.Authorization = new AuthenticationHeaderValue("Bearer", tokenToValidate);

            var response = await _httpClient.SendAsync(request);
            if (response.IsSuccessStatusCode)
            {
                _token = tokenToValidate;
                return true;
            }

            if (response.StatusCode == System.Net.HttpStatusCode.Unauthorized)
            {
                ClearToken();
            }

            return false;
        }
        catch
        {
            return false;
        }
    }

    // ---------------------------------------------------------
    // GET JOBS
    // ---------------------------------------------------------

    public async Task<List<Job>> GetJobsAsync(string token)
    {
        using var request = new HttpRequestMessage(
            HttpMethod.Get,
            GetEndpointUri("/jobs"));

        request.Headers.Authorization =
            new AuthenticationHeaderValue(
                "Bearer",
                token);

        var response =
            await _httpClient.SendAsync(request);

        var responseBody =
            await response.Content.ReadAsStringAsync();

        if (!response.IsSuccessStatusCode)
        {
            throw new Exception(
                $"Get jobs failed ({(int)response.StatusCode}): {responseBody}");
        }

        var jobs =
            JsonSerializer.Deserialize<List<Job>>(
                responseBody,
                new JsonSerializerOptions
                {
                    PropertyNameCaseInsensitive = true
                });

        return jobs ?? new List<Job>();
    }

    // ---------------------------------------------------------
    // GET SINGLE JOB
    // ---------------------------------------------------------

    public async Task<Job?> GetJobAsync(
        string jobId)
    {
        EnsureAuthenticated();

        var encodedJobId =
            Uri.EscapeDataString(jobId);

        using var request =
            CreateAuthenticatedRequest(
                HttpMethod.Get,
                $"/jobs/{encodedJobId}");

        try
        {
            var response =
                await _httpClient.SendAsync(request);

            var responseBody =
                await response.Content.ReadAsStringAsync();

            if (!response.IsSuccessStatusCode)
            {
                throw new Exception(
                    GetFriendlyHttpError(
                        response.StatusCode,
                        responseBody,
                        "Unable to load job details"));
            }

            return JsonSerializer.Deserialize<Job>(
                responseBody,
                _jsonOptions);
        }
        catch (HttpRequestException)
        {
            throw new Exception(
                "Unable to connect to the Moldflow backend.");
        }
        catch (TaskCanceledException)
        {
            throw new Exception(
                "The request timed out while loading the job.");
        }
    }

    // ---------------------------------------------------------
    // GET JOB EVENTS
    // ---------------------------------------------------------
    //
    // IMPORTANT:
    // The exact backend event schema was not included in the
    // provided source. This method is intentionally separated
    // so we can map JobEvent exactly after checking app.py.
    //

    public async Task<List<JobEvent>> GetJobEventsAsync(
        string jobId)
    {
        EnsureAuthenticated();

        var encodedJobId =
            Uri.EscapeDataString(jobId);

        using var request =
            CreateAuthenticatedRequest(
                HttpMethod.Get,
                $"/jobs/{encodedJobId}/events");

        try
        {
            var response =
                await _httpClient.SendAsync(request);
             
            var responseBody =
                await response.Content.ReadAsStringAsync();

            if (!response.IsSuccessStatusCode)
            {
                throw new Exception(
                    GetFriendlyHttpError(
                        response.StatusCode,
                        responseBody,
                        "Unable to load job events"));
            }

            var events =
                JsonSerializer.Deserialize<List<JobEvent>>(
                    responseBody,
                    _jsonOptions);

            return events ?? new List<JobEvent>();
        }
        catch (HttpRequestException)
        {
            throw new Exception(
                "Unable to connect to the Moldflow backend.");
        }
        catch (TaskCanceledException)
        {
            throw new Exception(
                "The request timed out while loading job events.");
        }
    }

    // ---------------------------------------------------------
    // HELPERS
    // ---------------------------------------------------------

    private HttpRequestMessage CreateAuthenticatedRequest(
        HttpMethod method,
        string url)
    {
        var request =
            new HttpRequestMessage(method, GetEndpointUri(url));

        request.Headers.Authorization =
            new AuthenticationHeaderValue(
                "Bearer",
                _token);

        return request;
    }

    private void EnsureAuthenticated()
    {
        if (string.IsNullOrWhiteSpace(_token))
        {
            throw new Exception(
                "Your session has expired. Please log in again.");
        }
    }

    private static string GetFriendlyHttpError(
        System.Net.HttpStatusCode statusCode,
        string responseBody,
        string defaultMessage)
    {
        return statusCode switch
        {
            System.Net.HttpStatusCode.Unauthorized =>
                "Your login session is no longer valid. Please log in again.",

            System.Net.HttpStatusCode.Forbidden =>
                "You do not have permission to access this information.",

            System.Net.HttpStatusCode.NotFound =>
                "The requested Moldflow information was not found.",

            System.Net.HttpStatusCode.BadRequest =>
                "The server rejected the request.",

            _ =>
                $"{defaultMessage} ({(int)statusCode})."
        };
    }
    public async Task RegisterDeviceAsync(string fcmToken)
    {
        EnsureAuthenticated();

        var deviceId =
            DeviceInfo.Current.Idiom == DeviceIdiom.Phone
                ? DeviceInfo.Current.Name
                : $"android-{DeviceInfo.Current.Idiom}";

        var data = new
        {
            device_id = deviceId,
            platform = "android",
            push_token = fcmToken
        };

        var json =
            JsonSerializer.Serialize(data);

        using var content =
            new StringContent(
                json,
                Encoding.UTF8,
                "application/json");

        using var request =
            CreateAuthenticatedRequest(
                HttpMethod.Post,
                "/devices/register");

        request.Content = content;

        var response =
            await _httpClient.SendAsync(request);

        var responseBody =
            await response.Content.ReadAsStringAsync();

        if (!response.IsSuccessStatusCode)
        {
            throw new Exception(
                $"Device registration failed ({(int)response.StatusCode}): {responseBody}");
        }
    }
    // ---------------------------------------------------------
    // CANCEL JOB
    // ---------------------------------------------------------

    public async Task<(bool Success, string Message)> CancelJobAsync(string jobId)
    {
        EnsureAuthenticated();

        var encodedJobId = Uri.EscapeDataString(jobId);

        using var request = CreateAuthenticatedRequest(
            HttpMethod.Post,
            $"/jobs/{encodedJobId}/cancel");

        request.Content = new StringContent("{}", Encoding.UTF8, "application/json");

        try
        {
            var response = await _httpClient.SendAsync(request);
            var responseBody = await response.Content.ReadAsStringAsync();

            if (response.IsSuccessStatusCode)
            {
                string message = "Cancellation requested. The solver will stop shortly.";
                try
                {
                    using var doc = JsonDocument.Parse(responseBody);
                    if (doc.RootElement.TryGetProperty("message", out var msgElem))
                    {
                        var m = msgElem.GetString();
                        if (!string.IsNullOrWhiteSpace(m))
                            message = m;
                    }
                }
                catch
                {
                    // Fallback to default message
                }

                return (true, message);
            }

            if (response.StatusCode == System.Net.HttpStatusCode.Conflict)
            {
                return (false, "This job has already finished and cannot be cancelled.");
            }

            return (false, GetFriendlyHttpError(
                response.StatusCode,
                responseBody,
                "Cancellation failed"));
        }
        catch (HttpRequestException)
        {
            return (false, "Unable to connect to the Moldflow backend.");
        }
        catch (TaskCanceledException)
        {
            return (false, "The cancellation request timed out.");
        }
    }

    // ---------------------------------------------------------
    // REMOVE JOB FROM LIST (soft delete — completed/failed/cancelled only)
    // ---------------------------------------------------------

    public async Task<(bool Success, string Message)> RemoveJobAsync(string jobId)
    {
        EnsureAuthenticated();

        var encodedJobId = Uri.EscapeDataString(jobId);

        using var request = CreateAuthenticatedRequest(
            HttpMethod.Delete,
            $"/jobs/{encodedJobId}");

        try
        {
            var response = await _httpClient.SendAsync(request);
            var responseBody = await response.Content.ReadAsStringAsync();

            if (response.IsSuccessStatusCode)
            {
                string message = "Job removed from your list.";
                try
                {
                    using var doc = JsonDocument.Parse(responseBody);
                    if (doc.RootElement.TryGetProperty("message", out var msgElem))
                    {
                        var m = msgElem.GetString();
                        if (!string.IsNullOrWhiteSpace(m))
                            message = m;
                    }
                }
                catch
                {
                    // Fallback to default message
                }

                return (true, message);
            }

            if (response.StatusCode == System.Net.HttpStatusCode.Conflict)
            {
                return (false, "This job is still active — cancel it before removing it.");
            }

            return (false, GetFriendlyHttpError(
                response.StatusCode,
                responseBody,
                "Removing the job failed"));
        }
        catch (HttpRequestException)
        {
            return (false, "Unable to connect to the Moldflow backend.");
        }
        catch (TaskCanceledException)
        {
            return (false, "The remove request timed out.");
        }
    }

    // ---------------------------------------------------------
    // NETWORK LICENSE APIS (STAGE 5/6)
    // ---------------------------------------------------------

    public async Task<LicenseOverviewResponse> GetLicenseOverviewAsync()
    {
        EnsureAuthenticated();

        using var request = CreateAuthenticatedRequest(
            HttpMethod.Get,
            "/licenses/overview");

        try
        {
            var response = await _httpClient.SendAsync(request);
            var responseBody = await response.Content.ReadAsStringAsync();

            if (!response.IsSuccessStatusCode)
            {
                throw new Exception(
                    GetFriendlyHttpError(
                        response.StatusCode,
                        responseBody,
                        "Unable to load network license overview"));
            }

            var overview = JsonSerializer.Deserialize<LicenseOverviewResponse>(
                responseBody,
                _jsonOptions);

            return overview ?? new LicenseOverviewResponse();
        }
        catch (HttpRequestException)
        {
            throw new Exception("Unable to connect to the Moldflow backend.");
        }
        catch (TaskCanceledException)
        {
            throw new Exception("The request timed out while loading license overview.");
        }
    }

    public async Task<List<LicenseServerSummary>> GetLicenseServersAsync()
    {
        EnsureAuthenticated();

        using var request = CreateAuthenticatedRequest(
            HttpMethod.Get,
            "/licenses/servers");

        try
        {
            var response = await _httpClient.SendAsync(request);
            var responseBody = await response.Content.ReadAsStringAsync();

            if (!response.IsSuccessStatusCode)
            {
                throw new Exception(
                    GetFriendlyHttpError(
                        response.StatusCode,
                        responseBody,
                        "Unable to load license servers"));
            }

            var servers = JsonSerializer.Deserialize<List<LicenseServerSummary>>(
                responseBody,
                _jsonOptions);

            return servers ?? new List<LicenseServerSummary>();
        }
        catch (HttpRequestException)
        {
            throw new Exception("Unable to connect to the Moldflow backend.");
        }
        catch (TaskCanceledException)
        {
            throw new Exception("The request timed out while loading license servers.");
        }
    }

    public async Task<LicenseServerDetail> GetLicenseServerAsync(string serverId)
    {
        EnsureAuthenticated();

        var encodedId = Uri.EscapeDataString(serverId);
        using var request = CreateAuthenticatedRequest(
            HttpMethod.Get,
            $"/licenses/servers/{encodedId}");

        try
        {
            var response = await _httpClient.SendAsync(request);
            var responseBody = await response.Content.ReadAsStringAsync();

            if (!response.IsSuccessStatusCode)
            {
                throw new Exception(
                    GetFriendlyHttpError(
                        response.StatusCode,
                        responseBody,
                        "Unable to load license server detail"));
            }

            var detail = JsonSerializer.Deserialize<LicenseServerDetail>(
                responseBody,
                _jsonOptions);

            return detail ?? throw new Exception("Failed to deserialize server detail.");
        }
        catch (HttpRequestException)
        {
            throw new Exception("Unable to connect to the Moldflow backend.");
        }
        catch (TaskCanceledException)
        {
            throw new Exception("The request timed out while loading server detail.");
        }
    }

    public async Task<List<ActiveConsumerItem>> GetLicenseConsumersAsync(string serverId)
    {
        EnsureAuthenticated();

        var encodedId = Uri.EscapeDataString(serverId);
        using var request = CreateAuthenticatedRequest(
            HttpMethod.Get,
            $"/licenses/servers/{encodedId}/consumers");

        try
        {
            var response = await _httpClient.SendAsync(request);
            var responseBody = await response.Content.ReadAsStringAsync();

            if (!response.IsSuccessStatusCode)
            {
                throw new Exception(
                    GetFriendlyHttpError(
                        response.StatusCode,
                        responseBody,
                        "Unable to load license consumers"));
            }

            var consumers = JsonSerializer.Deserialize<List<ActiveConsumerItem>>(
                responseBody,
                _jsonOptions);

            return consumers ?? new List<ActiveConsumerItem>();
        }
        catch (HttpRequestException)
        {
            throw new Exception("Unable to connect to the Moldflow backend.");
        }
        catch (TaskCanceledException)
        {
            throw new Exception("The request timed out while loading active consumers.");
        }
    }

    public async Task<LicenseHistoryResponse> GetLicenseHistoryAsync(
        string? serverId = null,
        string? featureCode = null,
        string? eventType = null,
        string? username = null,
        int limit = 50,
        int offset = 0)
    {
        EnsureAuthenticated();

        var queryParams = new List<string>
        {
            $"limit={limit}",
            $"offset={offset}"
        };

        if (!string.IsNullOrWhiteSpace(serverId))
            queryParams.Add($"server_id={Uri.EscapeDataString(serverId)}");
        if (!string.IsNullOrWhiteSpace(featureCode))
            queryParams.Add($"feature_code={Uri.EscapeDataString(featureCode)}");
        if (!string.IsNullOrWhiteSpace(eventType))
            queryParams.Add($"event_type={Uri.EscapeDataString(eventType)}");
        if (!string.IsNullOrWhiteSpace(username))
            queryParams.Add($"username={Uri.EscapeDataString(username)}");

        var queryString = string.Join("&", queryParams);

        using var request = CreateAuthenticatedRequest(
            HttpMethod.Get,
            $"/licenses/history?{queryString}");

        try
        {
            var response = await _httpClient.SendAsync(request);
            var responseBody = await response.Content.ReadAsStringAsync();

            if (!response.IsSuccessStatusCode)
            {
                throw new Exception(
                    GetFriendlyHttpError(
                        response.StatusCode,
                        responseBody,
                        "Unable to load license history"));
            }

            var history = JsonSerializer.Deserialize<LicenseHistoryResponse>(
                responseBody,
                _jsonOptions);

            return history ?? new LicenseHistoryResponse();
        }
        catch (HttpRequestException)
        {
            throw new Exception("Unable to connect to the Moldflow backend.");
        }
        catch (TaskCanceledException)
        {
            throw new Exception("The request timed out while loading license history.");
        }
    }
}