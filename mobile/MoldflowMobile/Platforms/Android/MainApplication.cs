using Android.App;
using Android.Runtime;
using MoldflowMobile;

#if DEBUG
[Application(
    UsesCleartextTraffic = true,
    Label = "Moldflow Mobile")]
#else
[Application(
    UsesCleartextTraffic = true,
    Label = "Moldflow Mobile")]
#endif

public class MainApplication : MauiApplication
{
    public MainApplication(
        IntPtr handle,
        JniHandleOwnership ownership)
        : base(handle, ownership)
    {
    }

    protected override MauiApp CreateMauiApp() =>
        MauiProgram.CreateMauiApp();
}