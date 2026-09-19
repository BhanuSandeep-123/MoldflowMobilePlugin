using System.Collections.ObjectModel;

namespace MoldflowMobile.ViewModels;

public class LicenseHistoryViewModel : BaseViewModel
{
    private readonly ApiService _apiService;
    private int _limit = 50;
    private int _offset = 0;
    private int _total = 0;
    private bool _isLoadingMore;

    private string? _selectedEventType = null;
    private string? _selectedServerId = null;

    public ObservableCollection<LicenseEventItem> Events { get; } = new();

    public int TotalEvents
    {
        get => _total;
        set => SetProperty(ref _total, value);
    }

    public bool HasEvents => Events.Count > 0;

    public bool IsEmpty => !IsBusy && !HasEvents;

    public bool CanLoadMore => Events.Count < TotalEvents && !_isLoadingMore && !IsBusy;

    public string? SelectedEventType
    {
        get => _selectedEventType;
        set
        {
            if (SetProperty(ref _selectedEventType, value))
            {
                _ = RefreshHistoryAsync();
            }
        }
    }

    public string? SelectedServerId
    {
        get => _selectedServerId;
        set
        {
            if (SetProperty(ref _selectedServerId, value))
            {
                _ = RefreshHistoryAsync();
            }
        }
    }

    public LicenseHistoryViewModel(ApiService apiService, string? initialServerId = null)
    {
        _apiService = apiService;
        _selectedServerId = initialServerId;
        Title = "License History";
    }

    public async Task LoadHistoryAsync(bool isPullToRefresh = false)
    {
        try
        {
            if (isPullToRefresh)
                IsRefreshing = true;
            else if (Events.Count == 0)
                IsBusy = true;

            ErrorMessage = null;
            _offset = 0;

            var response = await _apiService.GetLicenseHistoryAsync(
                serverId: _selectedServerId,
                featureCode: null,
                eventType: _selectedEventType,
                username: null,
                limit: _limit,
                offset: _offset);

            TotalEvents = response.Total;

            Events.Clear();
            foreach (var item in response.Items)
            {
                Events.Add(item);
            }

            OnPropertyChanged(nameof(HasEvents));
            OnPropertyChanged(nameof(IsEmpty));
            OnPropertyChanged(nameof(CanLoadMore));
        }
        catch (Exception ex)
        {
            ErrorMessage = ex.Message;
            System.Diagnostics.Debug.WriteLine($"Error loading history: {ex.Message}");
        }
        finally
        {
            IsBusy = false;
            IsRefreshing = false;
            OnPropertyChanged(nameof(IsEmpty));
            OnPropertyChanged(nameof(CanLoadMore));
        }
    }

    public async Task RefreshHistoryAsync()
    {
        await LoadHistoryAsync(isPullToRefresh: true);
    }

    public async Task LoadMoreAsync()
    {
        if (_isLoadingMore || !CanLoadMore)
            return;

        try
        {
            _isLoadingMore = true;
            OnPropertyChanged(nameof(CanLoadMore));

            _offset += _limit;

            var response = await _apiService.GetLicenseHistoryAsync(
                serverId: _selectedServerId,
                featureCode: null,
                eventType: _selectedEventType,
                username: null,
                limit: _limit,
                offset: _offset);

            TotalEvents = response.Total;

            foreach (var item in response.Items)
            {
                Events.Add(item);
            }

            OnPropertyChanged(nameof(HasEvents));
        }
        catch (Exception ex)
        {
            System.Diagnostics.Debug.WriteLine($"Error loading more history: {ex.Message}");
        }
        finally
        {
            _isLoadingMore = false;
            OnPropertyChanged(nameof(CanLoadMore));
        }
    }
}
