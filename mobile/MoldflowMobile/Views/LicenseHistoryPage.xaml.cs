using MoldflowMobile.ViewModels;

namespace MoldflowMobile.Views;

public partial class LicenseHistoryPage : ContentPage
{
    private static readonly Color FilterActiveBg = Color.FromArgb("#3157D5");
    private static readonly Color FilterActiveText = Colors.White;
    private static readonly Color FilterInactiveBg = Color.FromArgb("#E9ECF7");
    private static readonly Color FilterInactiveText = Color.FromArgb("#3157D5");

    private readonly LicenseHistoryViewModel _viewModel;

    public LicenseHistoryPage(ApiService apiService, string? initialServerId = null)
    {
        InitializeComponent();

        _viewModel = new LicenseHistoryViewModel(apiService, initialServerId);
        BindingContext = _viewModel;
    }

    protected override async void OnAppearing()
    {
        base.OnAppearing();

        await _viewModel.LoadHistoryAsync();
    }

    private async void OnRefreshClicked(object? sender, EventArgs e)
    {
        await _viewModel.RefreshHistoryAsync();
    }

    private async void OnRefreshViewRefreshing(object? sender, EventArgs e)
    {
        await _viewModel.RefreshHistoryAsync();
    }

    private async void OnLoadMoreClicked(object? sender, EventArgs e)
    {
        await _viewModel.LoadMoreAsync();
    }

    private void SetFilter(string? eventType, Button activeButton)
    {
        _viewModel.SelectedEventType = eventType;

        ResetButton(FilterAllBtn);
        ResetButton(FilterCheckoutBtn);
        ResetButton(FilterReturnBtn);
        ResetButton(FilterExhaustedBtn);
        ResetButton(FilterServerDownBtn);

        activeButton.BackgroundColor = FilterActiveBg;
        activeButton.TextColor = FilterActiveText;
    }

    private static void ResetButton(Button btn)
    {
        btn.BackgroundColor = FilterInactiveBg;
        btn.TextColor = FilterInactiveText;
    }

    private void OnFilterAllClicked(object? sender, EventArgs e) => SetFilter(null, FilterAllBtn);
    private void OnFilterCheckoutClicked(object? sender, EventArgs e) => SetFilter("CHECKOUT", FilterCheckoutBtn);
    private void OnFilterReturnClicked(object? sender, EventArgs e) => SetFilter("RETURN", FilterReturnBtn);
    private void OnFilterExhaustedClicked(object? sender, EventArgs e) => SetFilter("EXHAUSTED", FilterExhaustedBtn);
    private void OnFilterServerDownClicked(object? sender, EventArgs e) => SetFilter("SERVER_DOWN", FilterServerDownBtn);
}
