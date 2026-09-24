#if ANDROID

using Plugin.FirebasePushNotifications;

namespace MoldflowMobile;

public class FirebaseService
{
    private string? _fcmToken;
    public string? FcmToken
    {
        get
        {
            if (!string.IsNullOrWhiteSpace(_fcmToken))
                return _fcmToken;

            try
            {
                var currentToken = IFirebasePushNotification.Current?.Token;
                if (!string.IsNullOrWhiteSpace(currentToken))
                {
                    _fcmToken = currentToken;
                    return _fcmToken;
                }
            }
            catch
            {
            }

            var cached = Preferences.Default.Get<string?>("pending_fcm_token", null);
            if (!string.IsNullOrWhiteSpace(cached))
            {
                return cached;
            }

            return null;
        }
        private set => _fcmToken = value;
    }

    private bool _initialized;
    private bool _eventsSubscribed;

    public event EventHandler<string>? JobNotificationOpened;
    public event EventHandler<string>? TokenRefreshed;

    // Keep the constructor public because the existing App code uses:
    // new FirebaseService()
    public FirebaseService()
    {
    }

    public async Task<string?> WaitForTokenAsync(TimeSpan timeout)
    {
        var token = FcmToken;
        if (!string.IsNullOrWhiteSpace(token))
            return token;

        var start = DateTime.UtcNow;
        while (DateTime.UtcNow - start < timeout)
        {
            token = FcmToken;
            if (!string.IsNullOrWhiteSpace(token))
                return token;

            await Task.Delay(250);
        }

        return FcmToken;
    }

    public async Task InitializeAsync()
    {
        if (_initialized)
            return;

        try
        {
            try
            {
                var status = await Permissions.CheckStatusAsync<Permissions.PostNotifications>();
                if (status != PermissionStatus.Granted)
                {
                    await Permissions.RequestAsync<Permissions.PostNotifications>();
                }
            }
            catch (Exception permEx)
            {
                System.Diagnostics.Debug.WriteLine($"PostNotifications permission error: {permEx.Message}");
            }

            var permissions =
                INotificationPermissions.Current;

            await permissions.RequestPermissionAsync();

            var firebase =
                IFirebasePushNotification.Current;

            SubscribeToEvents(firebase);

            await firebase.RegisterForPushNotificationsAsync();

            // Check for token, retry briefly if initializing asynchronously
            for (int i = 0; i < 20; i++)
            {
                var t = firebase.Token;
                if (!string.IsNullOrWhiteSpace(t))
                {
                    FcmToken = t;
                    Preferences.Default.Set("pending_fcm_token", t);
                    break;
                }
                await Task.Delay(250);
            }

            if (string.IsNullOrWhiteSpace(FcmToken))
            {
                var cached = Preferences.Default.Get<string?>("pending_fcm_token", null);
                if (!string.IsNullOrWhiteSpace(cached))
                {
                    FcmToken = cached;
                }
            }

            if (!string.IsNullOrWhiteSpace(FcmToken))
            {
                System.Diagnostics.Debug.WriteLine(
                    "FCM token successfully obtained.");
                System.Diagnostics.Debug.WriteLine(
                    $"FCM token length: {FcmToken.Length}");
                _initialized = true;
            }
            else
            {
                System.Diagnostics.Debug.WriteLine(
                    "FCM token was not immediately available (will listen on TokenRefreshed).");
            }
        }
        catch (Exception ex)
        {
            System.Diagnostics.Debug.WriteLine(
                $"Firebase initialization failed: {ex}");
        }
    }

    private void SubscribeToEvents(
        IFirebasePushNotification firebase)
    {
        if (_eventsSubscribed)
            return;

        firebase.NotificationOpened += OnNotificationOpened;
        firebase.TokenRefreshed += OnTokenRefreshed;
        firebase.NotificationReceived += OnNotificationReceived;

        _eventsSubscribed = true;

        System.Diagnostics.Debug.WriteLine(
            "FCM NotificationOpened, NotificationReceived, and TokenRefreshed handlers subscribed.");
    }

    private void OnNotificationReceived(
        object? sender,
        FirebasePushNotificationDataEventArgs e)
    {
        try
        {
            string? title = null;
            string? body = null;
            string? jobId = null;
            string? jobName = null;
            string? status = null;
            string? percentStr = null;
            string? notificationType = null;

            if (e.Data != null)
            {
                if (e.Data.TryGetValue("title", out var t) && t != null && !string.IsNullOrWhiteSpace(t.ToString()))
                    title = t.ToString()!;
                if (e.Data.TryGetValue("body", out var b) && b != null && !string.IsNullOrWhiteSpace(b.ToString()))
                    body = b.ToString()!;

                if (e.Data.TryGetValue("job_id", out var jid) && jid != null)
                    jobId = jid.ToString();
                if (string.IsNullOrWhiteSpace(jobId) && e.Data.TryGetValue("jobId", out var jidc) && jidc != null)
                    jobId = jidc.ToString();

                if (e.Data.TryGetValue("job_name", out var jn) && jn != null)
                    jobName = jn.ToString();
                if (e.Data.TryGetValue("status", out var st) && st != null)
                    status = st.ToString();
                if (e.Data.TryGetValue("percent", out var pct) && pct != null)
                    percentStr = pct.ToString();
                if (e.Data.TryGetValue("notification_type", out var nt) && nt != null)
                    notificationType = nt.ToString();
            }

            // Fallback generation when backend title/body are not provided in data payload
            var effectiveStatus = (status ?? "").Trim().ToUpperInvariant();
            var effectiveType = (notificationType ?? "").Trim().ToUpperInvariant();
            var displayName = !string.IsNullOrWhiteSpace(jobName) ? jobName : (!string.IsNullOrWhiteSpace(jobId) ? jobId : "Study");

            int percent = 0;
            if (!string.IsNullOrWhiteSpace(percentStr) && int.TryParse(percentStr, out var parsedPct))
            {
                percent = Math.Max(0, Math.Min(100, parsedPct));
            }

            if (string.IsNullOrWhiteSpace(title))
            {
                if (effectiveStatus == "INPROGRESS" || effectiveType == "JOB_STARTED" || effectiveType == "STARTED")
                    title = "Moldflow Analysis Started";
                else if (effectiveStatus == "COMPLETED" || effectiveType == "JOB_COMPLETED")
                    title = "Moldflow Analysis Completed";
                else if (effectiveStatus == "FAILED" || effectiveType == "JOB_FAILED")
                    title = "Moldflow Analysis Failed";
                else if (effectiveStatus == "CANCELED" || effectiveType == "JOB_CANCELED")
                    title = "Moldflow Analysis Canceled";
                else
                    title = $"Moldflow: {displayName}";
            }

            if (string.IsNullOrWhiteSpace(body))
            {
                if (effectiveStatus == "INPROGRESS" || effectiveType == "JOB_STARTED" || effectiveType == "STARTED")
                {
                    if (percent > 0)
                        body = $"{displayName} is running ({percent}%).";
                    else
                        body = $"{displayName} has started running.";
                }
                else if (effectiveStatus == "COMPLETED" || effectiveType == "JOB_COMPLETED")
                {
                    body = $"{displayName} completed successfully.";
                }
                else if (effectiveStatus == "FAILED" || effectiveType == "JOB_FAILED")
                {
                    body = $"{displayName} failed.";
                }
                else if (effectiveStatus == "CANCELED" || effectiveType == "JOB_CANCELED")
                {
                    body = $"{displayName} was canceled.";
                }
                else
                {
                    body = $"{displayName}: {status ?? "Update"}";
                }
            }

            ShowLocalNotification(title, body, jobId);
        }
        catch (Exception ex)
        {
            System.Diagnostics.Debug.WriteLine($"Error in OnNotificationReceived: {ex}");
        }
    }

    private void ShowLocalNotification(string title, string body, string? jobId)
    {
        try
        {
            var context = Android.App.Application.Context;
            var intent = context.PackageManager?.GetLaunchIntentForPackage(context.PackageName ?? "");
            if (intent != null && !string.IsNullOrWhiteSpace(jobId))
            {
                intent.PutExtra("job_id", jobId);
            }

            var pendingIntent = Android.App.PendingIntent.GetActivity(
                context,
                0,
                intent,
                Android.App.PendingIntentFlags.UpdateCurrent | Android.App.PendingIntentFlags.Immutable);

            var builder = new AndroidX.Core.App.NotificationCompat.Builder(context, "moldflow_jobs")
                .SetContentTitle(title)
                .SetContentText(body)
                .SetSmallIcon(Android.Resource.Drawable.IcDialogInfo)
                .SetPriority(AndroidX.Core.App.NotificationCompat.PriorityMax)
                .SetDefaults((int)(Android.App.NotificationDefaults.Sound | Android.App.NotificationDefaults.Vibrate))
                .SetAutoCancel(true)
                .SetContentIntent(pendingIntent);

            var manager = AndroidX.Core.App.NotificationManagerCompat.From(context);
            manager.Notify(new Random().Next(1000, 9999), builder.Build());
        }
        catch (Exception ex)
        {
            System.Diagnostics.Debug.WriteLine($"Failed to show local notification: {ex}");
        }
    }

    private void OnTokenRefreshed(
        object? sender,
        FirebasePushNotificationTokenEventArgs e)
    {
        try
        {
            if (!string.IsNullOrWhiteSpace(e.Token))
            {
                FcmToken = e.Token;
                Preferences.Default.Set("pending_fcm_token", e.Token);
                System.Diagnostics.Debug.WriteLine($"FCM token refreshed: {FcmToken}");
                TokenRefreshed?.Invoke(this, e.Token);
            }
        }
        catch (Exception ex)
        {
            System.Diagnostics.Debug.WriteLine($"Error in OnTokenRefreshed: {ex}");
        }
    }

    private void OnNotificationOpened(
        object? sender,
        FirebasePushNotificationResponseEventArgs e)
    {
        try
        {
            System.Diagnostics.Debug.WriteLine(
                "FCM notification opened.");

            if (e.Data == null)
            {
                System.Diagnostics.Debug.WriteLine(
                    "FCM notification opened but Data is null.");

                return;
            }

            foreach (var item in e.Data)
            {
                System.Diagnostics.Debug.WriteLine(
                    $"FCM opened data: {item.Key} = {item.Value}");
            }

            string? jobId = null;

            if (e.Data.TryGetValue("job_id", out var jobIdValue))
            {
                jobId = jobIdValue?.ToString();
            }

            if (string.IsNullOrWhiteSpace(jobId) &&
                e.Data.TryGetValue("jobId", out var jobIdCamelValue))
            {
                jobId = jobIdCamelValue?.ToString();
            }

            if (string.IsNullOrWhiteSpace(jobId))
            {
                System.Diagnostics.Debug.WriteLine(
                    "FCM notification opened but no job_id was found.");

                return;
            }

            System.Diagnostics.Debug.WriteLine(
                $"FCM notification opened for job: {jobId}");

            JobNotificationOpened?.Invoke(
                this,
                jobId);
        }
        catch (Exception ex)
        {
            System.Diagnostics.Debug.WriteLine(
                $"FCM notification-open handling failed: {ex}");
        }
    }
}

#endif