using MoldflowMobile.ViewModels;

namespace MoldflowMobile.Views;

public partial class LicenseServerDetailPage : ContentPage
{
    private readonly ApiService _apiService;
    private readonly LicenseServerDetailViewModel _viewModel;

    public LicenseServerDetailPage(ApiService apiService, string serverId)
    {
        InitializeComponent();

        _apiService = apiService;
        _viewModel = new LicenseServerDetailViewModel(apiService, serverId);
        BindingContext = _viewModel;
    }

    protected override async void OnAppearing()
    {
        base.OnAppearing();

        await _viewModel.LoadServerDetailAsync();
    }

    private async void OnRefreshViewRefreshing(object? sender, EventArgs e)
    {
        await _viewModel.LoadServerDetailAsync(isPullToRefresh: true);
    }

    private async void OnViewConsumersClicked(object? sender, EventArgs e)
    {
        await Navigation.PushAsync(new LicenseConsumersPage(_apiService, _viewModel.ServerId, _viewModel.DisplayName));
    }
}
