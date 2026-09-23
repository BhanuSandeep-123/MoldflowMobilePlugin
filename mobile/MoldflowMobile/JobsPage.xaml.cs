using System.Collections.ObjectModel;

namespace MoldflowMobile;

public partial class JobsPage : ContentPage
{
    private enum DateFilterMode
    {
        Today,
        Yesterday,
        All,
        Custom
    }

    private enum StatusFilterMode
    {
        All,
        Active,
        Completed,
        Canceled,
        Failed
    }

    private static readonly Color FilterActiveBackground = Color.FromArgb("#3157D5");
    private static readonly Color FilterActiveText = Colors.White;
    private static readonly Color FilterInactiveBackground = Color.FromArgb("#E9ECF7");
    private static readonly Color FilterInactiveText = Color.FromArgb("#3157D5");

    private static readonly Color CardSelectedStroke = Color.FromArgb("#3157D5");
    private static readonly Color CardDefaultStroke = Color.FromArgb("#E2E6EF");

    private readonly ApiService _apiService;
    private readonly string _token;

    private CancellationTokenSource? _pollingCancellation;
    private bool _isLoading;

    private List<Job> _allJobs = new();
    private DateFilterMode _filterMode = DateFilterMode.All;
    private DateTime _customFilterDate = DateTime.Today;
    private StatusFilterMode _statusFilter = StatusFilterMode.All;

    // Bound to JobsCollectionView ONCE and updated in place (see
    // SyncDisplayedJobs) rather than reassigned on every poll -- replacing
    // ItemsSource wholesale is what was resetting the user's scroll
    // position to the top every ~2s.
    private readonly ObservableCollection<Job> _displayedJobs = new();

    public JobsPage(string token)
    {
        InitializeComponent();

        _apiService = new ApiService();
        _token = token;

        _apiService.SetToken(token);

        JobsCollectionView.ItemsSource = _displayedJobs;

        CalendarDatePicker.Date = DateTime.Today;

        UpdateFilterButtonStyles();
        UpdateSummaryCardStyles();
    }

    // =========================================================
    // PAGE APPEARING
    // =========================================================

    protected override async void OnAppearing()
    {
        base.OnAppearing();

        await LoadJobsAsync(showLoading: true);

        StartPolling();
    }

    // =========================================================
    // PAGE DISAPPEARING
    // =========================================================

    protected override void OnDisappearing()
    {
        base.OnDisappearing();

        StopPolling();
    }

    // =========================================================
    // LOAD JOBS
    // =========================================================

    private async Task LoadJobsAsync(bool showLoading = false)
    {
        if (_isLoading)
            return;

        try
        {
            _isLoading = true;

            if (showLoading)
            {
                LoadingIndicator.IsVisible = true;
                LoadingIndicator.IsRunning = true;
            }

            var jobs =
                await _apiService.GetJobsAsync(_token);

            if (jobs == null)
            {
                _allJobs = new List<Job>();
                SyncDisplayedJobs(_allJobs);
                JobCountLabel.Text = "0 jobs";
                UpdateSummaryCounts();
                return;
            }

            // GetJobsAsync already scopes results to the authenticated user
            // (server-side isolation) — the dashboard only ever summarizes
            // and filters within that already-authorized set.
            _allJobs = jobs;

            UpdateSummaryCounts();
            ApplyFiltersAndDisplay();
        }
        catch (Exception ex)
        {
            if (showLoading)
            {
                JobCountLabel.Text = "Failed to load jobs";

                await DisplayAlert(
                    "Jobs Error",
                    ex.Message,
                    "OK");
            }
        }
        finally
        {
            _isLoading = false;

            if (showLoading)
            {
                LoadingIndicator.IsVisible = false;
                LoadingIndicator.IsRunning = false;
            }
        }
    }

    // =========================================================
    // AUTOMATIC POLLING
    // =========================================================

    private void StartPolling()
    {
        if (_pollingCancellation != null)
            return;

        _pollingCancellation =
            new CancellationTokenSource();

        _ = PollJobsAsync(
            _pollingCancellation.Token);
    }

    private void StopPolling()
    {
        if (_pollingCancellation == null)
            return;

        _pollingCancellation.Cancel();
        _pollingCancellation.Dispose();

        _pollingCancellation = null;
    }

    private async Task PollJobsAsync(
        CancellationToken cancellationToken)
    {
        while (!cancellationToken.IsCancellationRequested)
        {
            try
            {
                await Task.Delay(
                    TimeSpan.FromSeconds(2),
                    cancellationToken);

                if (cancellationToken.IsCancellationRequested)
                    break;

                await MainThread.InvokeOnMainThreadAsync(
                    async () =>
                    {
                        await LoadJobsAsync();
                    });
            }
            catch (OperationCanceledException)
            {
                break;
            }
            catch
            {
                // Ignore temporary connection errors.
                // The next polling cycle will retry.
            }
        }
    }

    // =========================================================
    // JOB SELECTION / CARD TAP
    // =========================================================

    private async void OnJobSelected(
        object? sender,
        SelectionChangedEventArgs e)
    {
        if (e.CurrentSelection.FirstOrDefault() is Job selectedJob &&
            !string.IsNullOrWhiteSpace(selectedJob.JobId))
        {
            JobsCollectionView.SelectedItem = null;

            await Navigation.PushAsync(
                new JobDetailsPage(
                    _apiService,
                    selectedJob.JobId));
        }
    }

    // =========================================================
    // MANUAL REFRESH
    // =========================================================

    private async void OnRefreshClicked(
        object sender,
        EventArgs e)
    {
        await LoadJobsAsync(
            showLoading: true);
    }

    private async void OnLicensesClicked(
        object sender,
        EventArgs e)
    {
        await Navigation.PushAsync(
            new Views.NetworkLicensePage(_apiService));
    }

    // =========================================================
    // DASHBOARD SUMMARY (ACTIVE / COMPLETED / OVERALL)
    // =========================================================

    private void UpdateSummaryCounts()
    {
        var active = _allJobs.Count(j => j.IsRunning || j.IsQueued);
        var completed = _allJobs.Count(j => j.IsCompleted);
        var canceled = _allJobs.Count(j => j.IsCanceled);
        var failed = _allJobs.Count(j => j.IsFailed);

        ActiveCountLabel.Text = active.ToString();
        CompletedCountLabel.Text = completed.ToString();
        CanceledCountLabel.Text = canceled.ToString();
        FailedCountLabel.Text = failed.ToString();
        OverallCountLabel.Text = _allJobs.Count.ToString();
    }

    // =========================================================
    // DATE FILTER
    // =========================================================

    private static DateTime? JobLocalDate(Job job)
    {
        // Prefer the plugin-reported solve start time; fall back to when
        // the backend first saw the job (covers jobs still QUEUED/CREATED
        // that have no "started" timestamp yet).
        if (job.Started.HasValue && job.Started.Value > 0)
        {
            try
            {
                return DateTimeOffset
                    .FromUnixTimeSeconds((long)job.Started.Value)
                    .LocalDateTime
                    .Date;
            }
            catch
            {
                // Fall through to CreatedAt.
            }
        }

        if (job.CreatedAt.HasValue)
        {
            return job.CreatedAt.Value.LocalDateTime.Date;
        }

        return null;
    }

    private void ApplyFiltersAndDisplay()
    {
        IEnumerable<Job> filtered = _allJobs;

        if (_filterMode != DateFilterMode.All)
        {
            var targetDate = _filterMode switch
            {
                DateFilterMode.Today => DateTime.Today,
                DateFilterMode.Yesterday => DateTime.Today.AddDays(-1),
                DateFilterMode.Custom => _customFilterDate.Date,
                _ => DateTime.Today
            };

            filtered = filtered.Where(j => JobLocalDate(j) == targetDate);
        }

        filtered = _statusFilter switch
        {
            StatusFilterMode.Active => filtered.Where(j => j.IsRunning || j.IsQueued),
            StatusFilterMode.Completed => filtered.Where(j => j.IsCompleted),
            StatusFilterMode.Canceled => filtered.Where(j => j.IsCanceled),
            StatusFilterMode.Failed => filtered.Where(j => j.IsFailed),
            _ => filtered
        };

        var result = filtered.ToList();

        SyncDisplayedJobs(result);

        var dateSuffix = _filterMode switch
        {
            DateFilterMode.Today => " · Today",
            DateFilterMode.Yesterday => " · Yesterday",
            DateFilterMode.Custom => $" · {_customFilterDate:dd MMM yyyy}",
            _ => string.Empty
        };

        var statusSuffix = _statusFilter switch
        {
            StatusFilterMode.Active => " · Active",
            StatusFilterMode.Completed => " · Completed",
            StatusFilterMode.Canceled => " · Canceled",
            StatusFilterMode.Failed => " · Failed",
            _ => string.Empty
        };

        JobCountLabel.Text =
            $"{result.Count} job{(result.Count == 1 ? "" : "s")}{statusSuffix}{dateSuffix}";
    }

    // Updates _displayedJobs in place instead of replacing ItemsSource, so
    // CollectionView never sees a full reset -- existing rows keep their
    // position (no scroll jump) and only actually-changed rows re-render.
    // Order among items already on screen is intentionally left alone;
    // only genuinely new jobs are appended, so an update never reshuffles
    // what the user is currently looking at.
    private void SyncDisplayedJobs(List<Job> result)
    {
        var newIds = new HashSet<string>(result.Select(j => j.JobId));

        for (var i = _displayedJobs.Count - 1; i >= 0; i--)
        {
            if (!newIds.Contains(_displayedJobs[i].JobId))
                _displayedJobs.RemoveAt(i);
        }

        var resultById = result.ToDictionary(j => j.JobId);
        for (var i = 0; i < _displayedJobs.Count; i++)
        {
            if (resultById.TryGetValue(_displayedJobs[i].JobId, out var updated))
                _displayedJobs[i] = updated;
        }

        var existingIds = new HashSet<string>(_displayedJobs.Select(j => j.JobId));
        foreach (var job in result)
        {
            if (!existingIds.Contains(job.JobId))
                _displayedJobs.Add(job);
        }
    }

    private void UpdateSummaryCardStyles()
    {
        SetCardSelected(ActiveCard, _statusFilter == StatusFilterMode.Active);
        SetCardSelected(CompletedCard, _statusFilter == StatusFilterMode.Completed);
        SetCardSelected(CanceledCard, _statusFilter == StatusFilterMode.Canceled);
        SetCardSelected(FailedCard, _statusFilter == StatusFilterMode.Failed);
        SetCardSelected(OverallCard, _statusFilter == StatusFilterMode.All);
    }

    private static void SetCardSelected(Border card, bool isSelected)
    {
        card.Stroke = isSelected ? CardSelectedStroke : CardDefaultStroke;
    }

    private void OnActiveCardTapped(object? sender, TappedEventArgs e)
    {
        _statusFilter =
            _statusFilter == StatusFilterMode.Active
                ? StatusFilterMode.All
                : StatusFilterMode.Active;

        UpdateSummaryCardStyles();
        ApplyFiltersAndDisplay();
    }

    private void OnCompletedCardTapped(object? sender, TappedEventArgs e)
    {
        _statusFilter =
            _statusFilter == StatusFilterMode.Completed
                ? StatusFilterMode.All
                : StatusFilterMode.Completed;

        UpdateSummaryCardStyles();
        ApplyFiltersAndDisplay();
    }

    private void OnCanceledCardTapped(object? sender, TappedEventArgs e)
    {
        _statusFilter =
            _statusFilter == StatusFilterMode.Canceled
                ? StatusFilterMode.All
                : StatusFilterMode.Canceled;

        UpdateSummaryCardStyles();
        ApplyFiltersAndDisplay();
    }

    private void OnFailedCardTapped(object? sender, TappedEventArgs e)
    {
        _statusFilter =
            _statusFilter == StatusFilterMode.Failed
                ? StatusFilterMode.All
                : StatusFilterMode.Failed;

        UpdateSummaryCardStyles();
        ApplyFiltersAndDisplay();
    }

    private void OnOverallCardTapped(object? sender, TappedEventArgs e)
    {
        _statusFilter = StatusFilterMode.All;

        UpdateSummaryCardStyles();
        ApplyFiltersAndDisplay();
    }

    // =========================================================
    // REMOVE JOB FROM LIST (completed / failed / cancelled only)
    // =========================================================

    private async void OnRemoveJobClicked(object? sender, EventArgs e)
    {
        if (sender is not Button button ||
            button.BindingContext is not Job job ||
            string.IsNullOrWhiteSpace(job.JobId))
        {
            return;
        }

        var confirmed = await DisplayAlert(
            "Remove Job",
            $"Remove \"{job.Name}\" from your job list? It stays in the workstation's history — only the entry in your list disappears.",
            "Remove",
            "Cancel");

        if (!confirmed)
            return;

        button.IsEnabled = false;

        var (success, message) = await _apiService.RemoveJobAsync(job.JobId);

        if (success)
        {
            _allJobs = _allJobs.Where(j => j.JobId != job.JobId).ToList();
            UpdateSummaryCounts();
            ApplyFiltersAndDisplay();
        }
        else
        {
            button.IsEnabled = true;
            await DisplayAlert("Remove Job", message, "OK");
        }
    }

    private void UpdateFilterButtonStyles()
    {
        SetFilterButtonActive(TodayFilterButton, _filterMode == DateFilterMode.Today);
        SetFilterButtonActive(YesterdayFilterButton, _filterMode == DateFilterMode.Yesterday);
        SetFilterButtonActive(AllFilterButton, _filterMode == DateFilterMode.All);
    }

    private static void SetFilterButtonActive(Button button, bool isActive)
    {
        button.BackgroundColor = isActive ? FilterActiveBackground : FilterInactiveBackground;
        button.TextColor = isActive ? FilterActiveText : FilterInactiveText;
    }

    private void OnTodayFilterClicked(object sender, EventArgs e)
    {
        _filterMode = DateFilterMode.Today;
        UpdateFilterButtonStyles();
        ApplyFiltersAndDisplay();
    }

    private void OnYesterdayFilterClicked(object sender, EventArgs e)
    {
        _filterMode = DateFilterMode.Yesterday;
        UpdateFilterButtonStyles();
        ApplyFiltersAndDisplay();
    }

    private void OnAllFilterClicked(object sender, EventArgs e)
    {
        _filterMode = DateFilterMode.All;
        UpdateFilterButtonStyles();
        ApplyFiltersAndDisplay();
    }

    private void OnCalendarDateSelected(object sender, DateChangedEventArgs e)
    {
        _customFilterDate = (e.NewDate ?? DateTime.Today).Date;
        _filterMode = DateFilterMode.Custom;
        UpdateFilterButtonStyles();
        ApplyFiltersAndDisplay();
    }
}