using System.Collections.ObjectModel;

namespace MoldflowMobile.ViewModels;

public class LicenseConsumersViewModel : BaseViewModel
{
    private readonly ApiService _apiService;
    private readonly string _serverId;
    private string _serverName = string.Empty;

    public string ServerId => _serverId;

    public string ServerName
    {
        get => _serverName;
        set => SetProperty(ref _serverName, value);
    }

    public ObservableCollection<ActiveConsumerItem> Consumers { get; } = new();

    public int ConsumerCount => Consumers.Count;

    public bool HasConsumers => Consumers.Count > 0;

    public bool IsEmpty => !IsBusy && !HasConsumers;

    public LicenseConsumersViewModel(ApiService apiService, string serverId, string serverName = "")
    {
        _apiService = apiService;
        _serverId = serverId;
        _serverName = string.IsNullOrWhiteSpace(serverName) ? "License Server" : serverName;
        Title = "Active Users";
    }

    public async Task LoadConsumersAsync(bool isPullToRefresh = false)
    {
        try
        {
            if (isPullToRefresh)
                IsRefreshing = true;
            else if (Consumers.Count == 0)
                IsBusy = true;

            ErrorMessage = null;

            var items = await _apiService.GetLicenseConsumersAsync(_serverId);

            Consumers.Clear();
            foreach (var item in items)
            {
                Consumers.Add(item);
            }

            OnPropertyChanged(nameof(ConsumerCount));
            OnPropertyChanged(nameof(HasConsumers));
            OnPropertyChanged(nameof(IsEmpty));
        }
        catch (Exception ex)
        {
            ErrorMessage = ex.Message;
            System.Diagnostics.Debug.WriteLine($"Error loading consumers: {ex.Message}");
        }
        finally
        {
            IsBusy = false;
            IsRefreshing = false;
            OnPropertyChanged(nameof(IsEmpty));
        }
    }
}
