using Microsoft.Maui.Controls.Shapes;

namespace MoldflowMobile;

public partial class JobDetailsPage : ContentPage
{
    private readonly ApiService _apiService;
    private readonly string _jobId;

    public JobDetailsPage(ApiService apiService, string jobId)
    {
        InitializeComponent();

        _apiService = apiService;
        _jobId = jobId;
    }

    protected override async void OnAppearing()
    {
        base.OnAppearing();

        await LoadJobDetailsAsync();
    }

    // =========================================================
    // LOAD JOB DETAILS
    // =========================================================

    private async Task LoadJobDetailsAsync()
    {
        try
        {
            LoadingIndicator.IsVisible = true;
            LoadingIndicator.IsRunning = true;

            var job =  await _apiService.GetJobAsync(_jobId);

            if (job == null)
            {
                await DisplayAlertAsync("Job", "The requested job could not be found.", "OK");

                return;
            }

            DisplayJob(job);

            // Default state before events are loaded.
            FinishedLabel.Text =job.Finished ? "Finish time unavailable" : "Not finished";

            try
            {
                var events = await _apiService.GetJobEventsAsync(_jobId);

                DisplayActivity(events);

                UpdateFinishedTimeFromEvents(job, events);
            }
            catch (Exception ex)
            {
                ActivityLayout.Children.Clear();

                ActivityCountLabel.Text = "No activity";

                FinishedLabel.Text = job.Finished ? "Finish time unavailable"  : "Not finished";

                System.Diagnostics.Debug.WriteLine( $"Events loading failed: {ex}");
            }
        }
        catch (Exception ex)
        {
            await DisplayAlertAsync("Job Details",ex.Message,"OK");
        }
        finally
        {
            LoadingIndicator.IsVisible = false;
            LoadingIndicator.IsRunning = false;
        }
    }

    // =========================================================
    // DISPLAY JOB
    // =========================================================

    private void DisplayJob(Job job)
    {
        JobNameLabel.Text = string.IsNullOrWhiteSpace(job.Name) ? "Unnamed Job" : job.Name;

        JobTypeLabel.Text =  $"Type: {job.JobType}";

        StatusLabel.Text = job.DisplayStatus;

        // Cancel button: only visible for active INPROGRESS / RUNNING jobs
        CancelButton.IsVisible = job.IsRunning;
        CancelButton.IsEnabled = job.IsRunning;
        if (!job.IsRunning)
        {
            CancelStatusLabel.IsVisible = false;
        }

        PercentLabel.Text = $"{job.Percent:0}%";

        JobProgressBar.Progress =  job.ProgressValue;

        JobIdLabel.Text = job.JobId;

        StartedLabel.Text = job.StartedDisplay;

        // FinishedLabel is populated after the event history
        // is loaded, because the backend does not provide a
        // dedicated finished_at field.
        FinishedLabel.Text = job.Finished ? "Finish time unavailable" : "Not finished";

        MachineLabel.Text =string.IsNullOrWhiteSpace(job.MachineId) ? "Unknown" : job.MachineId;

        UpdatesLabel.Text = job.UpdateCount.ToString();

        // -----------------------------------------------------
        // SCM
        // -----------------------------------------------------

        ComputeSourceLabel.Text = GetValue(job.ComputeSource);

        ScmTypeLabel.Text = GetValue(job.ScmType);

        ScmUserLabel.Text = GetValue(job.ScmUser);

        WorkerLabel.Text = GetValue(job.Worker);

        ScmJobIdLabel.Text = GetValue(job.ScmJobId);

        ParentJobLabel.Text =
            GetValue(
                job.ParentJobId,
                "None");

        // -----------------------------------------------------
        // ERROR
        // -----------------------------------------------------

        if (!string.IsNullOrWhiteSpace(
                job.ErrorMessage))
        {
            ErrorCard.IsVisible = true;

            ErrorLabel.Text =
                job.ErrorMessage;
        }
        else
        {
            ErrorCard.IsVisible = false;
            ErrorLabel.Text = string.Empty;
        }
    }

    // =========================================================
    // FINISHED TIME FROM FINAL EVENT
    // =========================================================

    private void UpdateFinishedTimeFromEvents(
        Job job,
        IEnumerable<JobEvent>? events)
    {
        if (!job.Finished)
        {
            FinishedLabel.Text = "Not finished";
            return;
        }

        if (events == null)
        {
            FinishedLabel.Text =
                "Finish time unavailable";

            return;
        }

        var eventList =
            events
                .OrderBy(e => e.Id)
                .ToList();

        var terminalEvent =
            eventList
                .LastOrDefault(
                    e =>
                        string.Equals(
                            e.Status,
                            "COMPLETED",
                            StringComparison.OrdinalIgnoreCase)
                        ||
                        string.Equals(
                            e.Status,
                            "FAILED",
                            StringComparison.OrdinalIgnoreCase)
                        ||
                        string.Equals(
                            e.Status,
                            "CANCELED",
                            StringComparison.OrdinalIgnoreCase)
                        ||
                        string.Equals(
                            e.Status,
                            "CANCELLED",
                            StringComparison.OrdinalIgnoreCase)
                        ||
                        string.Equals(
                            e.Status,
                            "TIMEDOUT",
                            StringComparison.OrdinalIgnoreCase));

        if (terminalEvent?.ReceivedAt != null)
        {
            FinishedLabel.Text =
                terminalEvent
                    .ReceivedAt.Value
                    .LocalDateTime
                    .ToString(
                        "dd MMM yyyy, hh:mm tt");

            return;
        }

        FinishedLabel.Text =
            "Finish time unavailable";
    }

    // =========================================================
    // DISPLAY CLEAN ACTIVITY
    // =========================================================

    private void DisplayActivity(
        IEnumerable<JobEvent>? events)
    {
        ActivityLayout.Children.Clear();

        if (events == null)
        {
            ActivityCountLabel.Text =
                "No activity";

            AddEmptyActivity();

            return;
        }

        var eventList =
            events
                .OrderBy(e => e.Id)
                .ToList();

        if (eventList.Count == 0)
        {
            ActivityCountLabel.Text =
                "No activity";

            AddEmptyActivity();

            return;
        }

        var activities =
            BuildMeaningfulActivities(
                eventList);

        ActivityCountLabel.Text =
            $"{activities.Count} item{(activities.Count == 1 ? "" : "s")}";

        foreach (var activity in activities)
        {
            ActivityLayout.Children.Add(
                CreateActivityCard(activity));
        }
    }

    // =========================================================
    // FILTER REPETITIVE EVENTS
    // =========================================================

    private static List<ActivityItem>
        BuildMeaningfulActivities(
            List<JobEvent> events)
    {
        var activities =
            new List<ActivityItem>();

        bool startedAdded = false;
        int lastMilestone = 0;
        string? lastState = null;

        foreach (var ev in events)
        {
            var status =
                string.IsNullOrWhiteSpace(ev.Status)
                    ? "UNKNOWN"
                    : ev.Status
                        .Trim()
                        .ToUpperInvariant();

            var percent =
                ev.Percent.HasValue
                    ? Math.Clamp(
                        (int)Math.Round(ev.Percent.Value),
                        0,
                        100)
                    : -1;

            var time =
                ev.ReceivedAt.HasValue
                    ? ev.ReceivedAt.Value
                        .LocalDateTime
                        .ToString(
                            "dd MMM yyyy, hh:mm tt")
                    : string.Empty;

            // -------------------------------------------------
            // CREATED / QUEUED / PENDING
            // -------------------------------------------------

            if (status is
                "CREATED" or
                "QUEUED" or
                "PENDING")
            {
                if (lastState != status)
                {
                    activities.Add(
                        new ActivityItem
                        {
                            Title =
                                status == "CREATED"
                                    ? "Created"
                                    : status,

                            Details =
                                $"Status: {status}",

                            Time = time,
                            IsCompleted = false
                        });

                    lastState = status;
                }

                continue;
            }

            // -------------------------------------------------
            // RUNNING / INPROGRESS
            // -------------------------------------------------

            if (status is
                "INPROGRESS" or
                "RUNNING")
            {
                if (!startedAdded)
                {
                    activities.Add(
                        new ActivityItem
                        {
                            Title = "Started",
                            Details =
                                "Analysis is running",
                            Time = time,
                            IsCompleted = false
                        });

                    startedAdded = true;
                }

                var milestone =
                    GetMilestone(percent);

                if (milestone > lastMilestone &&
                    milestone >= 25)
                {
                    activities.Add(
                        new ActivityItem
                        {
                            Title =
                                $"Progress {milestone}%",

                            Details =
                                $"Analysis reached {milestone}%",

                            Time = time,
                            IsCompleted = false
                        });

                    lastMilestone = milestone;
                }

                lastState = status;

                continue;
            }

            // -------------------------------------------------
            // COMPLETED
            // -------------------------------------------------

            if (status == "COMPLETED")
            {
                activities.Add(
                    new ActivityItem
                    {
                        Title = "Completed",

                        Details =
                            "Analysis completed successfully",

                        Time = time,
                        IsCompleted = true
                    });

                lastState = status;

                continue;
            }

            // -------------------------------------------------
            // FAILED / CANCELLED / TIMEDOUT
            // -------------------------------------------------

            if (status is
                "FAILED" or
                "CANCELED" or
                "CANCELLED" or
                "TIMEDOUT")
            {
                var title =
                    status switch
                    {
                        "FAILED" => "Failed",
                        "CANCELED" => "Cancelled",
                        "CANCELLED" => "Cancelled",
                        "TIMEDOUT" => "Timed out",
                        _ => status
                    };

                var details =
                    !string.IsNullOrWhiteSpace(
                        ev.ErrorMessage)
                        ? ev.ErrorMessage
                        : $"Status: {status}";

                activities.Add(
                    new ActivityItem
                    {
                        Title = title,
                        Details = details,
                        Time = time,
                        IsCompleted = true
                    });

                lastState = status;

                continue;
            }
        }

        // If a job somehow has INPROGRESS events
        // but the Started item was not added, add it.
        if (events.Any(
                e =>
                    string.Equals(
                        e.Status,
                        "INPROGRESS",
                        StringComparison.OrdinalIgnoreCase)
                    ||
                    string.Equals(
                        e.Status,
                        "RUNNING",
                        StringComparison.OrdinalIgnoreCase)))
        {
            if (!activities.Any(
                    a => a.Title == "Started"))
            {
                activities.Insert(
                    0,
                    new ActivityItem
                    {
                        Title = "Started",
                        Details = "Analysis is running",
                        Time = string.Empty,
                        IsCompleted = false
                    });
            }
        }

        return activities;
    }

    // =========================================================
    // MILESTONES
    // =========================================================

    private static int GetMilestone(
        int percent)
    {
        if (percent < 25)
            return 0;

        if (percent < 50)
            return 25;

        if (percent < 75)
            return 50;

        if (percent < 90)
            return 75;

        if (percent < 100)
            return 90;

        return 100;
    }

    // =========================================================
    // CREATE ACTIVITY CARD
    // =========================================================

    private static Border CreateActivityCard(ActivityItem item)
    {
        var markerColor =item.IsCompleted
                ? Color.FromArgb("#3B82F6")
                : Color.FromArgb("#6366F1");

        var marker =
            new Border
            {
                WidthRequest = 14,
                HeightRequest = 14,
                Padding = 0,
                BackgroundColor = markerColor,
                StrokeThickness = 0,
                StrokeShape =
                    new RoundRectangle
                    {
                        CornerRadius = 7
                    },
                VerticalOptions =
                    LayoutOptions.Start
            };

        var title =
            new Label
            {
                Text = item.Title,
                FontSize = 15,
                FontAttributes =
                    FontAttributes.Bold
            };

        var details =
            new Label
            {
                Text = item.Details,
                FontSize = 12,
                TextColor = Colors.Gray
            };

        var time =
            new Label
            {
                Text = item.Time,
                FontSize = 11,
                TextColor = Colors.Gray,
                IsVisible =
                    !string.IsNullOrWhiteSpace(
                        item.Time)
            };

        var text =
            new VerticalStackLayout
            {
                Spacing = 3,
                Children =
                {
                    title,
                    details,
                    time
                }
            };

        var content =
            new Grid
            {
                ColumnDefinitions =
                    new ColumnDefinitionCollection
                    {
                        new ColumnDefinition
                        {
                            Width = 24
                        },
                        new ColumnDefinition
                        {
                            Width = GridLength.Star
                        }
                    },

                ColumnSpacing = 8
            };

        content.Add(
            marker,
            0,
            0);

        content.Add(
            text,
            1,
            0);

        return new Border
        {
            Padding = 12,

            StrokeShape =
                new RoundRectangle
                {
                    CornerRadius = 12
                },

            BackgroundColor =
                Application.Current?
                    .RequestedTheme ==
                    AppTheme.Dark
                    ? Color.FromArgb("#1B1E24")
                    : Colors.White,

            Content = content
        };
    }

    // =========================================================
    // EMPTY ACTIVITY
    // =========================================================

    private void AddEmptyActivity()
    {
        ActivityLayout.Children.Add(
            new Border
            {
                Padding = 16,

                StrokeShape =
                    new RoundRectangle
                    {
                        CornerRadius = 12
                    },

                Content =
                    new Label
                    {
                        Text =
                            "No activity available.",
                        FontSize = 14,
                        TextColor = Colors.Gray
                    }
            });
    }

    // =========================================================
    // HELPERS
    // =========================================================

    private static string GetValue(string? value,string fallback = "Not available")
    {
        return string.IsNullOrWhiteSpace(value)
            ? fallback
            : value;
    }

    // =========================================================
    // CANCEL ANALYSIS
    // =========================================================

    private async void OnCancelClicked(object sender, EventArgs e)
    {
        bool confirm = await DisplayAlertAsync(
            "Cancel Analysis",
            "Are you sure you want to cancel this running analysis?\n\nThe solver on the workstation will be stopped.",
            "Yes, Cancel",
            "Keep Running");

        if (!confirm)
            return;

        try
        {
            CancelButton.IsEnabled = false;
            CancelStatusLabel.Text = "Requesting cancellation...";
            CancelStatusLabel.TextColor = Color.FromArgb("#DC2626");
            CancelStatusLabel.IsVisible = true;

            var (success, message) = await _apiService.CancelJobAsync(_jobId);

            if (success)
            {
                CancelStatusLabel.Text = "Cancellation requested. The solver will stop shortly.";
                CancelStatusLabel.TextColor = Color.FromArgb("#16A34A");
                CancelStatusLabel.IsVisible = true;
                await DisplayAlertAsync("Cancellation Requested", message, "OK");
            }
            else
            {
                CancelButton.IsEnabled = true;
                CancelStatusLabel.Text = message;
                CancelStatusLabel.TextColor = Color.FromArgb("#DC2626");
                CancelStatusLabel.IsVisible = true;
                await DisplayAlertAsync("Cancel Job", message, "OK");
            }
        }
        catch (Exception ex)
        {
            CancelButton.IsEnabled = true;
            CancelStatusLabel.IsVisible = false;
            await DisplayAlertAsync("Cancel Job", ex.Message, "OK");
        }
    }
}

// =============================================================
// ACTIVITY UI MODEL
// =============================================================

public class ActivityItem
{
    public string Title { get; set; } = string.Empty;

    public string Details { get; set; } = string.Empty;

    public string Time { get; set; } = string.Empty;

    public bool IsCompleted { get; set; }
}