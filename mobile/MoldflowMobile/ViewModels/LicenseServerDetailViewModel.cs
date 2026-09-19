using System.Collections.ObjectModel;

namespace MoldflowMobile.ViewModels;

public class LicenseServerDetailViewModel : BaseViewModel
{
    private readonly ApiService _apiService;
    private readonly string _serverId;
    private LicenseServerDetail? _serverDetail;

    public string ServerId => _serverId;

    public LicenseServerDetail? ServerDetail
    {
        get => _serverDetail;
        private set
        {
            if (SetProperty(ref _serverDetail, value))
            {
                OnPropertyChanged(nameof(Hostname));
                OnPropertyChanged(nameof(DisplayName));
                OnPropertyChanged(nameof(StatusText));
                OnPropertyChanged(nameof(DataStateText));
                OnPropertyChanged(nameof(LastPollText));
                OnPropertyChanged(nameof(IsOnline));
                OnPropertyChanged(nameof(IsDataAvailable));
                OnPropertyChanged(nameof(HasPackages));
                OnPropertyChanged(nameof(ErrorDetails));
                OnPropertyChanged(nameof(HasErrorDetails));
            }
        }
    }

    public ObservableCollection<LicensePackageDetail> Packages { get; } = new();

    public string Hostname => ServerDetail?.Hostname ?? string.Empty;

    public string DisplayName => ServerDetail?.DisplayName ?? (!string.IsNullOrWhiteSpace(Hostname) ? Hostname : "License Server");

    public string StatusText => ServerDetail?.HealthDisplayText ?? "UNKNOWN";

    public string DataStateText => ServerDetail?.DataState ?? "UNKNOWN";

    public string LastPollText => ServerDetail?.FormattedLastPoll ?? "Never";

    public bool IsOnline => ServerDetail?.IsOnline ?? false;

    public bool IsDataAvailable => ServerDetail?.IsDataAvailable ?? false;

    public bool HasPackages => Packages.Count > 0;

    public string? ErrorDetails => ServerDetail?.LastErrorMessage;

    public bool HasErrorDetails => !string.IsNullOrWhiteSpace(ErrorDetails);

    public LicenseServerDetailViewModel(ApiService apiService, string serverId)
    {
        _apiService = apiService;
        _serverId = serverId;
        Title = "Server Details";
    }

    public async Task LoadServerDetailAsync(bool isPullToRefresh = false)
    {
        try
        {
            if (isPullToRefresh)
                IsRefreshing = true;
            else if (ServerDetail == null)
                IsBusy = true;

            ErrorMessage = null;

            var detail = await _apiService.GetLicenseServerAsync(_serverId);
            ServerDetail = detail;

            Packages.Clear();
            if (detail.Packages != null)
            {
                foreach (var pkg in detail.Packages)
                {
                    Packages.Add(pkg);
                }
            }
        }
        catch (Exception ex)
        {
            ErrorMessage = ex.Message;
            System.Diagnostics.Debug.WriteLine($"Error loading server detail: {ex.Message}");
        }
        finally
        {
            IsBusy = false;
            IsRefreshing = false;
        }
    }
}
