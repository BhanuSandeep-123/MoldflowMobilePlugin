namespace MoldflowMobile;

public partial class App : Application
{
    public static string? PendingJobId { get; set; }

    public App()
    {
        InitializeComponent();
    }

    protected override Window CreateWindow(
        IActivationState? activationState)
    {
        return new Window(
            new NavigationPage(
                new MainPage()));
    }
}