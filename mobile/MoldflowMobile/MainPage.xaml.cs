namespace MoldflowMobile;

public partial class MainPage : ContentPage
{
    private readonly ApiService _apiService;

#if ANDROID
    private FirebaseService? _firebaseService;
    private Task? _firebaseInitializationTask;
#endif

    private bool _isCheckingSession;

    public MainPage()
    {
        InitializeComponent();

        ServerUrlEntry.Text = ApiService.GetBaseUrl();
        _apiService = new ApiService();
    }

    protected override async void OnAppearing()
    {
        base.OnAppearing();

#if ANDROID
        // Start Firebase initialization once.
        _firebaseInitializationTask ??=
            InitializeFirebaseAsync();
#endif

        if (!string.IsNullOrWhiteSpace(App.PendingJobId))
        {
            var pendingId = App.PendingJobId;
            App.PendingJobId = null;
            await OpenJobFromNotificationAsync(pendingId);
            return;
        }

        if (!_isCheckingSession)
        {
            await CheckExistingSessionAsync();
        }
    }

    private async Task CheckExistingSessionAsync()
    {
        try
        {
            _isCheckingSession = true;

            var savedToken = await _apiService.GetSavedTokenAsync();
            if (string.IsNullOrWhiteSpace(savedToken))
                return;

            LoadingIndicator.IsVisible = true;
            LoadingIndicator.IsRunning = true;
            StatusLabel.Text = "Restoring session...";

            var isValid = await _apiService.ValidateSessionAsync(savedToken);
            if (isValid)
            {
                StatusLabel.Text = "Session restored.";

#if ANDROID
                if (_firebaseInitializationTask != null)
                {
                    await _firebaseInitializationTask;
                }

                if (_firebaseService != null && !string.IsNullOrWhiteSpace(_firebaseService.FcmToken))
                {
                    try
                    {
                        await _apiService.RegisterDeviceAsync(_firebaseService.FcmToken);
                    }
                    catch (Exception ex)
                    {
                        System.Diagnostics.Debug.WriteLine($"Device registration on auto-login failed: {ex}");
                    }
                }
#endif

                await Navigation.PushAsync(new JobsPage(savedToken));
            }
            else
            {
                StatusLabel.Text = "Please log in to continue.";
            }
        }
        catch (Exception ex)
        {
            System.Diagnostics.Debug.WriteLine($"Auto-login check failed: {ex.Message}");
            StatusLabel.Text = "Please log in.";
        }
        finally
        {
            LoadingIndicator.IsVisible = false;
            LoadingIndicator.IsRunning = false;
        }
    }

#if ANDROID

    // =========================================================
    // FIREBASE INITIALIZATION
    // =========================================================

    private async Task InitializeFirebaseAsync()
    {
        try
        {
            _firebaseService =
                new FirebaseService();

            // When the user taps a notification, FirebaseService
            // extracts the job_id and sends it here.
            _firebaseService.JobNotificationOpened +=
                async (_, jobId) =>
                {
                    await OpenJobFromNotificationAsync(jobId);
                };

            _firebaseService.TokenRefreshed +=
                async (_, token) =>
                {
                    try
                    {
                        var jwt = _apiService.GetToken() ?? await _apiService.GetSavedTokenAsync();
                        if (!string.IsNullOrWhiteSpace(jwt))
                        {
                            await _apiService.RegisterDeviceAsync(token);
                            System.Diagnostics.Debug.WriteLine("FCM token registered on TokenRefreshed.");
                        }
                    }
                    catch (Exception ex)
                    {
                        System.Diagnostics.Debug.WriteLine($"Failed to register FCM token on TokenRefreshed: {ex}");
                    }
                };

            await _firebaseService.InitializeAsync();

            System.Diagnostics.Debug.WriteLine(
                "Firebase initialization completed.");
        }
        catch (Exception ex)
        {
            System.Diagnostics.Debug.WriteLine(
                $"Firebase initialization failed: {ex}");
        }
    }

#endif

    // =========================================================
    // NOTIFICATION -> JOB DETAILS
    // =========================================================

    private async Task OpenJobFromNotificationAsync(
        string jobId)
    {
        if (string.IsNullOrWhiteSpace(jobId))
        {
            System.Diagnostics.Debug.WriteLine(
                "Notification tap received without a job_id.");

            return;
        }

        try
        {
            await MainThread.InvokeOnMainThreadAsync(
                async () =>
                {
                    System.Diagnostics.Debug.WriteLine(
                        $"Opening job from notification: {jobId}");

                    var token =
                        _apiService.GetToken() ??
                        await _apiService.GetSavedTokenAsync();

                    if (string.IsNullOrWhiteSpace(token))
                    {
                        System.Diagnostics.Debug.WriteLine(
                            "Notification tap received, but no JWT is available.");

                        await DisplayAlert(
                            "Moldflow Job",
                            "Please log in before opening a job from a notification.",
                            "OK");

                        return;
                    }

                    // Reuse the same authenticated ApiService.
                    _apiService.SetToken(token);

                    var detailsPage =
                        new JobDetailsPage(
                            _apiService,
                            jobId);

                    await Navigation.PushAsync(
                        detailsPage);
                });
        }
        catch (Exception ex)
        {
            System.Diagnostics.Debug.WriteLine(
                $"Failed to open job from notification: {ex}");
        }
    }

    // =========================================================
    // LOGIN
    // =========================================================

    private async void OnLoginClicked(
        object sender,
        EventArgs e)
    {
        var serverUrl =
            ServerUrlEntry.Text?.Trim();

        if (!string.IsNullOrWhiteSpace(serverUrl))
        {
            ApiService.SetBaseUrl(serverUrl);
        }

        var email =
            EmailEntry.Text?.Trim();

        var password =
            PasswordEntry.Text;

        if (string.IsNullOrWhiteSpace(email))
        {
            StatusLabel.Text =
                "Please enter your email.";

            return;
        }

        if (string.IsNullOrWhiteSpace(password))
        {
            StatusLabel.Text =
                "Please enter your password.";

            return;
        }

        try
        {
            LoginButton.IsEnabled = false;

            LoadingIndicator.IsVisible = true;
            LoadingIndicator.IsRunning = true;

            StatusLabel.Text =
                "Logging in...";

            // -------------------------------------------------
            // 1. Login
            // -------------------------------------------------

            var token =
                await _apiService.LoginAsync(
                    email,
                    password);

#if ANDROID

            // -------------------------------------------------
            // 2. Make sure Firebase initialization finished
            // -------------------------------------------------

            if (_firebaseInitializationTask != null)
            {
                await _firebaseInitializationTask;
            }

            // -------------------------------------------------
            // 3. Register FCM token with backend
            // -------------------------------------------------

            if (_firebaseService != null &&
                !string.IsNullOrWhiteSpace(
                    _firebaseService.FcmToken))
            {
                try
                {
                    await _apiService.RegisterDeviceAsync(
                        _firebaseService.FcmToken);

                    System.Diagnostics.Debug.WriteLine(
                        "Device registration completed.");
                }
                catch (Exception ex)
                {
                    // Push registration failure should not prevent
                    // the user from logging in.
                    System.Diagnostics.Debug.WriteLine(
                        $"Device registration failed: {ex}");
                }
            }
            else
            {
                System.Diagnostics.Debug.WriteLine(
                    "FCM token is not available.");
            }

#endif

            StatusLabel.Text =
                "Login successful.";

            // -------------------------------------------------
            // 4. Open Jobs page
            // -------------------------------------------------

            await Navigation.PushAsync(
                new JobsPage(token));
        }
        catch (Exception ex)
        {
            StatusLabel.Text =
                "Login failed.";

            await DisplayAlert(
                "Login Error",
                ex.Message,
                "OK");
        }
        finally
        {
            LoginButton.IsEnabled = true;

            LoadingIndicator.IsVisible = false;
            LoadingIndicator.IsRunning = false;
        }
    }
}