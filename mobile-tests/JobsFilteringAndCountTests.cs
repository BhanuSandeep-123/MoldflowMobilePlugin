using MoldflowMobile;
using Xunit;

namespace MoldflowMobile.Tests;

public class JobsFilteringAndCountTests
{
    private static List<Job> CreateSampleJobs()
    {
        var jobs = new List<Job>();

        // 67 Completed jobs
        for (int i = 1; i <= 67; i++)
        {
            jobs.Add(new Job
            {
                JobId = $"comp-{i}",
                Name = $"Completed Study {i}",
                Status = "COMPLETED",
                Percent = 100.0,
                Finished = true
            });
        }

        // 3 Canceled jobs (including both CANCELED and CANCELLED spellings)
        jobs.Add(new Job
        {
            JobId = "canc-1",
            Name = "Canceled Study 1",
            Status = "CANCELED",
            Percent = 15.0
        });
        jobs.Add(new Job
        {
            JobId = "canc-2",
            Name = "Canceled Study 2",
            Status = "CANCELLED",
            Percent = 42.0
        });
        jobs.Add(new Job
        {
            JobId = "canc-3",
            Name = "Canceled Study 3",
            Status = "CANCELED",
            Percent = 0.0
        });

        // 1 Failed job (matching the exact database record)
        jobs.Add(new Job
        {
            JobId = "a13be17a-edb6-4231-8c18-27361b4d30c0",
            Name = "unoteam_study_4.sdy",
            Status = "FAILED",
            Percent = 50.0,
            ErrorMessage = "Analysis terminated with errors"
        });

        return jobs;
    }

    [Fact]
    public void FailedCount_CalculatedDynamicallyFromJobData()
    {
        var jobs = CreateSampleJobs();

        // Dynamically counted via predicate matching JobsPage.UpdateSummaryCounts
        var failedCount = jobs.Count(j => j.IsFailed);
        Assert.Equal(1, failedCount);

        // Dynamically recalculates when new failed jobs arrive
        jobs.Add(new Job { JobId = "fail-2", Status = "FAILED" });
        jobs.Add(new Job { JobId = "fail-3", Status = "failed" }); // case-insensitive check
        Assert.Equal(3, jobs.Count(j => j.IsFailed));
    }

    [Fact]
    public void FailedFilter_OnlyReturnsJobsWithFailedStatus()
    {
        var jobs = CreateSampleJobs();

        // Filter applied matching JobsPage.ApplyFiltersAndDisplay
        var failedJobs = jobs.Where(j => j.IsFailed).ToList();

        Assert.Single(failedJobs);
        Assert.Equal("a13be17a-edb6-4231-8c18-27361b4d30c0", failedJobs[0].JobId);
        Assert.Equal("FAILED", failedJobs[0].DisplayStatus);
        Assert.True(failedJobs.All(j => j.DisplayStatus == "FAILED"));
    }

    [Fact]
    public void Overall_StillIncludesFailed()
    {
        var jobs = CreateSampleJobs();

        // Overall represents the total unarchived job inventory
        var overallCount = jobs.Count;
        Assert.Equal(71, overallCount);

        // Verify the failed job is part of overall
        Assert.Contains(jobs, j => j.JobId == "a13be17a-edb6-4231-8c18-27361b4d30c0" && j.IsFailed);
    }

    [Fact]
    public void Canceled_DoesNotIncludeFailed()
    {
        var jobs = CreateSampleJobs();

        // IsCanceled must never be true for FAILED jobs
        var failedJob = jobs.First(j => j.DisplayStatus == "FAILED");
        Assert.False(failedJob.IsCanceled, "IsCanceled should be false for FAILED jobs");
        Assert.True(failedJob.IsFailed, "IsFailed should be true for FAILED jobs");

        // IsFailed must never be true for CANCELED jobs
        var canceledJobs = jobs.Where(j => j.IsCanceled).ToList();
        Assert.Equal(3, canceledJobs.Count);
        foreach (var cj in canceledJobs)
        {
            Assert.False(cj.IsFailed, $"IsFailed should be false for {cj.DisplayStatus} jobs");
            Assert.True(cj.IsCanceled, $"IsCanceled should be true for {cj.DisplayStatus} jobs");
        }
    }

    [Fact]
    public void ExistingCompletedCanceledActiveCounts_RemainUnchanged()
    {
        var jobs = CreateSampleJobs();

        var active = jobs.Count(j => j.IsRunning || j.IsQueued);
        var completed = jobs.Count(j => j.IsCompleted);
        var canceled = jobs.Count(j => j.IsCanceled);
        var failed = jobs.Count(j => j.IsFailed);
        var overall = jobs.Count;

        Assert.Equal(0, active);
        Assert.Equal(67, completed);
        Assert.Equal(3, canceled);
        Assert.Equal(1, failed);
        Assert.Equal(71, overall);

        // Complete mathematical partition
        Assert.Equal(overall, active + completed + canceled + failed);
    }

    [Theory]
    [InlineData("COMPLETED", true)]
    [InlineData("FAILED", true)]
    [InlineData("CANCELED", true)]
    [InlineData("CANCELLED", true)]
    [InlineData("TIMEDOUT", true)]
    [InlineData("RUNNING", false)]
    [InlineData("INPROGRESS", false)]
    [InlineData("QUEUED", false)]
    [InlineData("CREATED", false)]
    [InlineData("PENDING", false)]
    public void IsRemovable_MaintainsTerminalEligibility(string status, bool expectedRemovable)
    {
        var job = new Job { Status = status };
        Assert.Equal(expectedRemovable, job.IsRemovable);
    }
}
