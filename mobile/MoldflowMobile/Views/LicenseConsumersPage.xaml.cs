using MoldflowMobile.ViewModels;

namespace MoldflowMobile.Views;

public partial class LicenseConsumersPage : ContentPage
{
    private readonly LicenseConsumersViewModel _viewModel;

    public LicenseConsumersPage(ApiService apiService, string serverId, string serverName = "")
    {
        InitializeComponent();

        _viewModel = new LicenseConsumersViewModel(apiService, serverId, serverName);
        BindingContext = _viewModel;
    }

    protected override async void OnAppearing()
    {
        base.OnAppearing();

        await _viewModel.LoadConsumersAsync();
    }

    private async void OnRefreshClicked(object? sender, EventArgs e)
    {
        await _viewModel.LoadConsumersAsync(isPullToRefresh: true);
    }

    private async void OnRefreshViewRefreshing(object? sender, EventArgs e)
    {
        await _viewModel.LoadConsumersAsync(isPullToRefresh: true);
    }
}
