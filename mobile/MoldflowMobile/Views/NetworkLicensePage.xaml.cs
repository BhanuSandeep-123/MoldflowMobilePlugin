using MoldflowMobile.ViewModels;

namespace MoldflowMobile.Views;

public partial class NetworkLicensePage : ContentPage
{
    private readonly ApiService _apiService;
    private readonly NetworkLicenseViewModel _viewModel;

    public NetworkLicensePage(ApiService apiService)
    {
        InitializeComponent();

        _apiService = apiService;
        _viewModel = new NetworkLicenseViewModel(apiService);
        BindingContext = _viewModel;
    }

    protected override async void OnAppearing()
    {
        base.OnAppearing();

        await _viewModel.LoadOverviewAsync();
        _viewModel.StartPeriodicRefresh();
    }

    protected override void OnDisappearing()
    {
        base.OnDisappearing();

        _viewModel.StopPeriodicRefresh();
    }

    private async void OnManualRefreshClicked(object? sender, EventArgs e)
    {
        await _viewModel.LoadOverviewAsync(isPullToRefresh: true);
    }

    private async void OnRefreshViewRefreshing(object? sender, EventArgs e)
    {
        await _viewModel.LoadOverviewAsync(isPullToRefresh: true);
    }

    private async void OnHistoryClicked(object? sender, EventArgs e)
    {
        await Navigation.PushAsync(new LicenseHistoryPage(_apiService));
    }


    private async void OnServerConsumersClicked(object? sender, EventArgs e)
    {
        if (sender is Button button && button.BindingContext is ServerOverviewItem server && !string.IsNullOrWhiteSpace(server.ServerId))
        {
            await Navigation.PushAsync(new LicenseConsumersPage(_apiService, server.ServerId, server.DisplayName));
        }
    }
}
