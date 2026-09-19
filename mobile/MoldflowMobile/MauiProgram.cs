using Microsoft.Extensions.Logging;

#if ANDROID
using Plugin.FirebasePushNotifications;
using Plugin.FirebasePushNotifications.Model.Queues;
#endif

namespace MoldflowMobile;

public static class MauiProgram
{
    public static MauiApp CreateMauiApp()
    {
        var builder = MauiApp.CreateBuilder();

#if ANDROID
        builder
            .UseMauiApp<App>()
            .UseFirebasePushNotifications(options =>
            {
                // Keep notification-open information available when the
                // application is launched from a notification tap.
                options.QueueFactory = new PersistentQueueFactory();
            })
#else
        builder
            .UseMauiApp<App>()
#endif
            .ConfigureFonts(fonts =>
            {
                fonts.AddFont(
                    "OpenSans-Regular.ttf",
                    "OpenSansRegular");

                fonts.AddFont(
                    "OpenSans-Semibold.ttf",
                    "OpenSansSemibold");
            });

#if DEBUG
        builder.Logging.AddDebug();
#endif

        return builder.Build();
    }
}