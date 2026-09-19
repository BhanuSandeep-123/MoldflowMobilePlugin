using System.Text.Json;
using Xunit;
using MoldflowMobile;

namespace MoldflowMobile.Tests;

public class NetworkLicenseTests
{
    private static readonly JsonSerializerOptions JsonOptions = new()
    {
        PropertyNameCaseInsensitive = true
    };

    // ========================================================================
    // 1. OVERVIEW & INDEPENDENT SERVER POOLS
    // ========================================================================

    [Fact]
    public void Overview_ServersRemainIndependent_AndDoNotShareSeats()
    {
        var json = """
        {
            "servers": [
                {
                    "server_id": "srv-a-uuid",
                    "hostname": "laptop-ca2qn87f",
                    "display_name": "License Server A",
                    "status": "UP",
                    "data_state": "AVAILABLE",
                    "products": [
                        {
                            "feature_code": "77800MFS_T_F",
                            "product_family": "Synergy",
                            "display_name": "Autodesk Moldflow Synergy",
                            "total_issued": 1,
                            "in_use": 1,
                            "available": 0,
                            "status": "EXHAUSTED",
                            "catalog_status": "VERIFIED"
                        },
                        {
                            "feature_code": "77400MFIA_T_F",
                            "product_family": "Insight",
                            "display_name": "Autodesk Moldflow Insight",
                            "total_issued": 3,
                            "in_use": 0,
                            "available": 3,
                            "status": "AVAILABLE",
                            "catalog_status": "VERIFIED"
                        }
                    ]
                },
                {
                    "server_id": "srv-b-uuid",
                    "hostname": "desktop-23tmnr6",
                    "display_name": "License Server B",
                    "status": "UP",
                    "data_state": "AVAILABLE",
                    "products": [
                        {
                            "feature_code": "77800MFS_T_F",
                            "product_family": "Synergy",
                            "display_name": "Autodesk Moldflow Synergy",
                            "total_issued": 4,
                            "in_use": 0,
                            "available": 4,
                            "status": "AVAILABLE",
                            "catalog_status": "VERIFIED"
                        },
                        {
                            "feature_code": "77400MFIA_T_F",
                            "product_family": "Insight",
                            "display_name": "Autodesk Moldflow Insight",
                            "total_issued": 12,
                            "in_use": 0,
                            "available": 12,
                            "status": "AVAILABLE",
                            "catalog_status": "VERIFIED"
                        },
                        {
                            "feature_code": "76800MFAA_T_F",
                            "product_family": "MFAA",
                            "display_name": "Unknown Moldflow Product (MFAA)",
                            "total_issued": 4,
                            "in_use": 0,
                            "available": 4,
                            "status": "AVAILABLE",
                            "catalog_status": "UNKNOWN"
                        }
                    ]
                }
            ],
            "environment_inventory": {
                "notice": "Aggregate inventory across independent license servers. Licenses are not pooled.",
                "is_pooled": false,
                "total_servers": 2,
                "servers_up": 2,
                "servers_down": 0,
                "active_checkouts_total": 1,
                "products": [
                    {
                        "feature_code": "77800MFS_T_F",
                        "product_family": "Synergy",
                        "display_name": "Autodesk Moldflow Synergy",
                        "total_inventory": 5,
                        "current_inventory_used": 1,
                        "inventory_available": 4,
                        "server_count": 2
                    },
                    {
                        "feature_code": "77400MFIA_T_F",
                        "product_family": "Insight",
                        "display_name": "Autodesk Moldflow Insight",
                        "total_inventory": 15,
                        "current_inventory_used": 0,
                        "inventory_available": 15,
                        "server_count": 2
                    },
                    {
                        "feature_code": "76800MFAA_T_F",
                        "product_family": "MFAA",
                        "display_name": "Unknown Moldflow Product (MFAA)",
                        "total_inventory": 4,
                        "current_inventory_used": 0,
                        "inventory_available": 4,
                        "server_count": 1
                    }
                ]
            }
        }
        """;

        var response = JsonSerializer.Deserialize<LicenseOverviewResponse>(json, JsonOptions);
        Assert.NotNull(response);
        Assert.Equal(2, response.Servers.Count);

        // Server A checks
        var srvA = response.Servers.First(s => s.ServerId == "srv-a-uuid");
        Assert.Equal("laptop-ca2qn87f", srvA.Hostname);
        Assert.True(srvA.IsOnline);
        Assert.True(srvA.IsDataAvailable);
        Assert.Equal(2, srvA.Products.Count);

        var srvASynergy = srvA.Products.First(p => p.FeatureCode == "77800MFS_T_F");
        Assert.Equal(1, srvASynergy.TotalIssued);
        Assert.Equal(1, srvASynergy.InUse);
        Assert.Equal(0, srvASynergy.Available);
        Assert.Equal("EXHAUSTED", srvASynergy.StatusBadgeText);

        // Server B checks
        var srvB = response.Servers.First(s => s.ServerId == "srv-b-uuid");
        Assert.Equal("desktop-23tmnr6", srvB.Hostname);
        Assert.Equal(3, srvB.Products.Count);

        var srvBSynergy = srvB.Products.First(p => p.FeatureCode == "77800MFS_T_F");
        Assert.Equal(4, srvBSynergy.TotalIssued);
        Assert.Equal(0, srvBSynergy.InUse);
        Assert.Equal(4, srvBSynergy.Available);
        Assert.Equal("AVAILABLE", srvBSynergy.StatusBadgeText);

        // Verify independent pools: Server A being exhausted did NOT take from Server B
        Assert.NotEqual(srvASynergy.Available, srvBSynergy.Available);
        Assert.Equal(0, srvASynergy.Available);
        Assert.Equal(4, srvBSynergy.Available);
    }

    [Fact]
    public void EnvironmentInventory_IncludesExplicitNonPooledNotice()
    {
        var inv = new EnvironmentInventory
        {
            Notice = "Aggregate inventory across independent license servers. Licenses are not pooled.",
            IsPooled = false,
            TotalServers = 2,
            ServersUp = 2,
            ActiveCheckoutsTotal = 1
        };

        Assert.False(inv.IsPooled);
        Assert.Contains("not pooled", inv.Notice, StringComparison.OrdinalIgnoreCase);
        Assert.Contains("independent", inv.Notice, StringComparison.OrdinalIgnoreCase);
    }

    // ========================================================================
    // 2. PACKAGE AS PHYSICAL SEAT AUTHORITY & COMPONENT AS METADATA
    // ========================================================================

    [Fact]
    public void ServerDetail_PackageNesting_NeverDoubleCountsComponentsAsSeats()
    {
        var json = """
        {
            "server_id": "srv-a-uuid",
            "hostname": "laptop-ca2qn87f",
            "display_name": "License Server A",
            "lmgrd_port": 27000,
            "vendor_daemon": "adskflex",
            "status": "UP",
            "data_state": "AVAILABLE",
            "is_active": true,
            "packages": [
                {
                    "feature_code": "77800MFS_T_F",
                    "product_name": "Autodesk Moldflow Synergy",
                    "product_family": "Synergy",
                    "display_name": "Autodesk Moldflow Synergy",
                    "total_issued": 1,
                    "in_use": 1,
                    "available": 0,
                    "utilization_pct": 100.0,
                    "status": "EXHAUSTED",
                    "catalog_status": "VERIFIED",
                    "components": [
                        {
                            "feature_code": "88232MFS_2027_0F",
                            "product_name": "Autodesk Moldflow Synergy",
                            "product_family": "Synergy",
                            "year_version": "2027",
                            "catalog_status": "VERIFIED",
                            "in_use": 1
                        },
                        {
                            "feature_code": "88068MFS_2026_0F",
                            "product_name": "Autodesk Moldflow Synergy",
                            "product_family": "Synergy",
                            "year_version": "2026",
                            "catalog_status": "VERIFIED",
                            "in_use": null
                        }
                    ]
                }
            ]
        }
        """;

        var detail = JsonSerializer.Deserialize<LicenseServerDetail>(json, JsonOptions);
        Assert.NotNull(detail);
        Assert.Single(detail.Packages);

        var pkg = detail.Packages[0];
        // Physical seat authority is solely in TotalIssued of the PACKAGE
        Assert.Equal(1, pkg.TotalIssued);
        Assert.Equal(1, pkg.InUse);
        Assert.Equal(0, pkg.Available);

        // Components are metadata nested under the package
        Assert.Equal(2, pkg.Components.Count);
        Assert.Equal("88232MFS_2027_0F", pkg.Components[0].FeatureCode);
        Assert.Equal("2027", pkg.Components[0].YearVersion);
        Assert.Equal("v2027", pkg.Components[0].DisplayVersion);

        // Ensure components count does NOT inflate total physical seats
        Assert.Equal(1, pkg.TotalIssued); // Stays 1, not 1 + 2 = 3
    }

    // ========================================================================
    // 3. UNKNOWN PRODUCT HANDLING (MFAA)
    // ========================================================================

    [Fact]
    public void UnknownProduct_MFAA_DoesNotInventCommercialName()
    {
        var pkg = new LicensePackageDetail
        {
            FeatureCode = "76800MFAA_T_F",
            ProductName = null,
            ProductFamily = "MFAA",
            CatalogStatus = "UNKNOWN",
            TotalIssued = 4,
            InUse = 0,
            Available = 4
        };

        // Must display neutral designation, never an invented commercial trademark
        Assert.Contains("Unknown Moldflow Product", pkg.EffectiveDisplayName);
        Assert.Contains("MFAA", pkg.EffectiveDisplayName);
        Assert.DoesNotContain("Autodesk Moldflow Advisor", pkg.EffectiveDisplayName);
        Assert.DoesNotContain("Ultimate", pkg.EffectiveDisplayName);
    }

    // ========================================================================
    // 4. ACTIVE CONSUMERS (SECURITY & PHYSICAL SEAT 1:1)
    // ========================================================================

    [Fact]
    public void Consumers_ConsolidatedPhysicalCheckout_OmitsSensitiveFields()
    {
        var json = """
        [
            {
                "checkout_id": "chk-001",
                "server_id": "srv-a-uuid",
                "server_hostname": "laptop-ca2qn87f",
                "username": "UnoTEAM-0144",
                "machine_name": "LAPTOP-CA2QN87F",
                "product_name": "Autodesk Moldflow Synergy",
                "package_feature": "77800MFS_T_F",
                "component_feature": "88232MFS_2027_0F",
                "version": "2027",
                "checkout_time": "Fri 9/18 10:00 AM",
                "is_borrowed": false
            }
        ]
        """;

        var consumers = JsonSerializer.Deserialize<List<ActiveConsumerItem>>(json, JsonOptions);
        Assert.NotNull(consumers);
        Assert.Single(consumers);

        var consumer = consumers[0];
        Assert.Equal("UnoTEAM-0144", consumer.Username);
        Assert.Equal("LAPTOP-CA2QN87F", consumer.MachineName);
        Assert.Equal("Autodesk Moldflow Synergy", consumer.DisplayProductTitle);
        Assert.Equal("Version: 2027", consumer.DisplayVersion);
        Assert.Equal("NETWORK", consumer.BorrowedBadgeText);
        Assert.Equal("Fri 9/18 10:00 AM", consumer.CheckoutTimeDisplay);

        // Security check: Verify that ActiveConsumerItem class definition does NOT have PID or ServerHandle properties
        var properties = typeof(ActiveConsumerItem).GetProperties().Select(p => p.Name.ToLowerInvariant()).ToList();
        Assert.DoesNotContain("pid", properties);
        Assert.DoesNotContain("serverhandle", properties);
        Assert.DoesNotContain("server_handle", properties);
        Assert.DoesNotContain("licensefilepath", properties);
        Assert.DoesNotContain("license_file_path", properties);
    }

    // ========================================================================
    // 5. HEALTH STATES: DOWN, VENDOR_DOWN, STALE, UNKNOWN
    // ========================================================================

    [Theory]
    [InlineData("UP", "AVAILABLE", "ONLINE", false)]
    [InlineData("DOWN", "UNAVAILABLE", "SERVER DOWN", true)]
    [InlineData("VENDOR_DOWN", "UNAVAILABLE", "VENDOR DOWN", true)]
    [InlineData("STALE", "UNAVAILABLE", "STALE", true)]
    [InlineData("UNKNOWN", "UNAVAILABLE", "UNKNOWN", true)]
    public void ServerHealthStates_AreDistinct_AndUnavailableServersNeverShowZeroAsAvailable(
        string status, string dataState, string expectedHealthText, bool isUnavailable)
    {
        var server = new ServerOverviewItem
        {
            ServerId = "test-srv",
            Hostname = "test-host",
            Status = status,
            DataState = dataState
        };

        Assert.Equal(expectedHealthText, server.HealthDisplayText);
        Assert.Equal(isUnavailable, server.IsUnavailable);

        if (isUnavailable)
        {
            Assert.False(server.IsDataAvailable);
            // Must indicate unavailable, never 0 free seats
            Assert.Contains("unavailable", server.ServerSubtitle, StringComparison.OrdinalIgnoreCase);
        }
    }

    // ========================================================================
    // 6. PARTIAL SERVER OUTAGE RESILIENCE
    // ========================================================================

    [Fact]
    public void PartialOutage_HealthyServerRendersNormally_WhileDownServerShowsUnavailable()
    {
        var json = """
        {
            "servers": [
                {
                    "server_id": "srv-up",
                    "hostname": "laptop-ca2qn87f",
                    "display_name": "License Server A",
                    "status": "UP",
                    "data_state": "AVAILABLE",
                    "products": [
                        {
                            "feature_code": "77800MFS_T_F",
                            "product_name": "Autodesk Moldflow Synergy",
                            "total_issued": 1,
                            "in_use": 1,
                            "available": 0,
                            "status": "EXHAUSTED"
                        }
                    ]
                },
                {
                    "server_id": "srv-down",
                    "hostname": "desktop-down-host",
                    "display_name": "Offline Server",
                    "status": "DOWN",
                    "data_state": "UNAVAILABLE",
                    "products": []
                }
            ],
            "environment_inventory": {
                "total_servers": 2,
                "servers_up": 1,
                "servers_down": 1,
                "active_checkouts_total": 1,
                "products": []
            }
        }
        """;

        var overview = JsonSerializer.Deserialize<LicenseOverviewResponse>(json, JsonOptions);
        Assert.NotNull(overview);
        Assert.Equal(2, overview.Servers.Count);

        var healthy = overview.Servers.First(s => s.ServerId == "srv-up");
        Assert.True(healthy.IsDataAvailable);
        Assert.False(healthy.IsUnavailable);
        Assert.Equal("ONLINE", healthy.HealthDisplayText);
        Assert.Single(healthy.Products);

        var offline = overview.Servers.First(s => s.ServerId == "srv-down");
        Assert.False(offline.IsDataAvailable);
        Assert.True(offline.IsUnavailable);
        Assert.Equal("SERVER DOWN", offline.HealthDisplayText);
        Assert.Empty(offline.Products);
    }

    // ========================================================================
    // 7. LICENSE HISTORY EVENTS & FORMATTING
    // ========================================================================

    [Theory]
    [InlineData("CHECKOUT", "License Checked Out")]
    [InlineData("RETURN", "License Returned")]
    [InlineData("EXHAUSTED", "Licenses Exhausted")]
    [InlineData("AVAILABLE", "Licenses Available Again")]
    [InlineData("SERVER_UP", "Server Came Online")]
    [InlineData("SERVER_DOWN", "Server Went Offline")]
    [InlineData("VENDOR_DOWN", "Vendor Service Down")]
    public void HistoryEvents_MapToHumanFriendlyTitles(string eventType, string expectedTitle)
    {
        var ev = new LicenseEventItem
        {
            EventId = "ev-1",
            ServerId = "srv-1",
            EventType = eventType,
            Username = "UnoTEAM-0144",
            DetectedAt = "2026-09-18T10:00:00Z"
        };

        Assert.Equal(expectedTitle, ev.FriendlyTitle);
        Assert.Contains("UnoTEAM-0144", ev.Subtitle);
    }

    [Fact]
    public void HistoryEvents_UnknownFutureEventType_SafelyRenderedWithoutCrash()
    {
        var ev = new LicenseEventItem
        {
            EventId = "ev-future",
            ServerId = "srv-1",
            EventType = "HEARTBEAT_ACK",
            DetectedAt = "2026-09-18T10:00:00Z"
        };

        Assert.Equal("Event: HEARTBEAT_ACK", ev.FriendlyTitle);
    }
}
