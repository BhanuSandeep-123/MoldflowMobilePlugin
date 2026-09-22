using System.Collections.ObjectModel;

namespace MoldflowMobile.ViewModels;

public class NetworkLicenseViewModel : BaseViewModel
{
    private readonly ApiService _apiService;
    private CancellationTokenSource? _periodicRefreshCts;
    private bool _isRefreshingData;

    private EnvironmentInventory _environmentInventory = new();
    private string _lastUpdatedText = "Not updated yet";

    public EnvironmentInventory EnvironmentInventory
    {
        get => _environmentInventory;
        private set
        {
            if (SetProperty(ref _environmentInventory, value))
            {
                OnPropertyChanged(nameof(NoticeText));
                OnPropertyChanged(nameof(TotalServers));
                OnPropertyChanged(nameof(ServersUp));
                OnPropertyChanged(nameof(ServersDown));
                OnPropertyChanged(nameof(ActiveCheckoutsTotal));
                OnPropertyChanged(nameof(TotalServersDisplay));
                OnPropertyChanged(nameof(ServersUpDisplay));
                OnPropertyChanged(nameof(ServersDownDisplay));
                OnPropertyChanged(nameof(ActiveCheckoutsDisplay));
            }
        }
    }

    public ObservableCollection<ServerOverviewItem> Servers { get; } = new();

    public ObservableCollection<AggregateProductInventory> AggregateProducts { get; } = new();

    private bool _hasLoadedData;
    public bool HasLoadedData
    {
        get => _hasLoadedData;
        private set => SetProperty(ref _hasLoadedData, value);
    }

    public string TotalServersDisplay => !HasLoadedData || HasError ? "-" : TotalServers.ToString();
    public string ServersUpDisplay => !HasLoadedData || HasError ? "-" : ServersUp.ToString();
    public string ServersDownDisplay => !HasLoadedData || HasError ? "-" : ServersDown.ToString();
    public string ActiveCheckoutsDisplay => !HasLoadedData || HasError ? "-" : ActiveCheckoutsTotal.ToString();

    public string NoticeText => EnvironmentInventory.Notice;

    public int TotalServers => Servers.Count;

    public int ServersUp => Servers.Count(s => s.IsOnline);

    public int ServersDown => Servers.Count(s => !s.IsOnline);

    public int ActiveCheckoutsTotal => EnvironmentInventory.ActiveCheckoutsTotal;

    public string LastUpdatedText
    {
        get => _lastUpdatedText;
        set => SetProperty(ref _lastUpdatedText, value);
    }

    public NetworkLicenseViewModel(ApiService apiService)
    {
        _apiService = apiService;
        Title = "Network Licenses";
    }

    // =========================================================
    // LOAD DATA
    // =========================================================

    public async Task LoadOverviewAsync(bool isPullToRefresh = false)
    {
        if (_isRefreshingData)
            return;

        try
        {
            _isRefreshingData = true;

            if (isPullToRefresh)
                IsRefreshing = true;
            else if (Servers.Count == 0)
                IsBusy = true;

            ErrorMessage = null;

            var response = await _apiService.GetLicenseOverviewAsync();

            EnvironmentInventory = response.EnvironmentInventory ?? new EnvironmentInventory();
            HasLoadedData = true;

            // Sync aggregate products
            AggregateProducts.Clear();
            if (response.EnvironmentInventory?.Products != null)
            {
                foreach (var prod in response.EnvironmentInventory.Products)
                {
                    AggregateProducts.Add(prod);
                }
            }

            // Sync servers in place
            SyncServers(response.Servers ?? new List<ServerOverviewItem>());

            LastUpdatedText = $"Updated at {DateTime.Now:hh:mm:ss tt}";
        }
        catch (Exception ex)
        {
            ErrorMessage = ex.Message;
            System.Diagnostics.Debug.WriteLine($"Error loading license overview: {ex.Message}");
        }
        finally
        {
            _isRefreshingData = false;
            IsBusy = false;
            IsRefreshing = false;
            OnPropertyChanged(nameof(TotalServersDisplay));
            OnPropertyChanged(nameof(ServersUpDisplay));
            OnPropertyChanged(nameof(ServersDownDisplay));
            OnPropertyChanged(nameof(ActiveCheckoutsDisplay));
        }
    }

    public void SyncServers(List<ServerOverviewItem> incoming)
    {
        Servers.Clear();
        foreach (var server in incoming)
        {
            if (IsUnknownOrSyntheticServer(server))
                continue;

            Servers.Add(server);
        }

        OnPropertyChanged(nameof(TotalServers));
        OnPropertyChanged(nameof(ServersUp));
        OnPropertyChanged(nameof(ServersDown));
        OnPropertyChanged(nameof(TotalServersDisplay));
        OnPropertyChanged(nameof(ServersUpDisplay));
        OnPropertyChanged(nameof(ServersDownDisplay));
    }

    public static bool IsUnknownOrSyntheticServer(ServerOverviewItem? server)
    {
        if (server == null)
            return true;

        var host = (server.Hostname ?? string.Empty).Trim();
        var display = (server.DisplayName ?? string.Empty).Trim();

        return string.IsNullOrEmpty(host)
            || string.Equals(host, "UNKNOWN", StringComparison.OrdinalIgnoreCase)
            || string.Equals(display, "UNKNOWN", StringComparison.OrdinalIgnoreCase);
    }

    // =========================================================
    // CONTROLLED PERIODIC REFRESH (45 seconds while page is visible)
    // =========================================================

    public void StartPeriodicRefresh()
    {
        StopPeriodicRefresh();

        _periodicRefreshCts = new CancellationTokenSource();
        var token = _periodicRefreshCts.Token;

        _ = Task.Run(async () =>
        {
            while (!token.IsCancellationRequested)
            {
                try
                {
                    await Task.Delay(TimeSpan.FromSeconds(45), token);

                    if (token.IsCancellationRequested)
                        break;

                    await MainThread.InvokeOnMainThreadAsync(async () =>
                    {
                        await LoadOverviewAsync(isPullToRefresh: false);
                    });
                }
                catch (OperationCanceledException)
                {
                    break;
                }
                catch (Exception ex)
                {
                    System.Diagnostics.Debug.WriteLine($"Periodic license refresh error: {ex.Message}");
                }
            }
        }, token);
    }

    public void StopPeriodicRefresh()
    {
        if (_periodicRefreshCts != null)
        {
            _periodicRefreshCts.Cancel();
            _periodicRefreshCts.Dispose();
            _periodicRefreshCts = null;
        }
    }
}
