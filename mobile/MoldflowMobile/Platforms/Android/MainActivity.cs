#if ANDROID

using Android.App;
using Android.Content.PM;
using Android.OS;

namespace MoldflowMobile;

[Activity(
    Theme = "@style/Maui.SplashTheme",
    MainLauncher = true,
    LaunchMode = LaunchMode.SingleTask,
    ConfigurationChanges =
        ConfigChanges.ScreenSize |
        ConfigChanges.Orientation |
        ConfigChanges.UiMode |
        ConfigChanges.ScreenLayout |
        ConfigChanges.SmallestScreenSize |
        ConfigChanges.Density)]
public class MainActivity : MauiAppCompatActivity
{
    protected override void OnCreate(Bundle? savedInstanceState)
    {
        base.OnCreate(savedInstanceState);

        CreateNotificationChannel();
        CheckNotificationIntent(Intent);
    }

    protected override void OnNewIntent(Android.Content.Intent? intent)
    {
        base.OnNewIntent(intent);
        CheckNotificationIntent(intent);
    }

    private void CheckNotificationIntent(Android.Content.Intent? intent)
    {
        if (intent == null)
            return;

        var jobId = intent.GetStringExtra("job_id") ?? intent.GetStringExtra("jobId");
        if (!string.IsNullOrWhiteSpace(jobId))
        {
            App.PendingJobId = jobId;
        }
    }

    private void CreateNotificationChannel()
    {
        if (Build.VERSION.SdkInt >= BuildVersionCodes.O)
        {
            var channel = new NotificationChannel(
                "moldflow_jobs",
                "Moldflow Analysis Alerts",
                NotificationImportance.High)
            {
                Description = "Notifications for Moldflow analysis progress and completion"
            };

            channel.EnableVibration(true);
            channel.EnableLights(true);

            var notificationManager =
                (NotificationManager?)GetSystemService(NotificationService);

            notificationManager?.CreateNotificationChannel(channel);
        }
    }
}

#endif