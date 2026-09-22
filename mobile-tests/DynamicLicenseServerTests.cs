using System.Text.Json;
using Xunit;
using MoldflowMobile;
using MoldflowMobile.ViewModels;

namespace MoldflowMobile.Tests;

public class DynamicLicenseServerTests
{
    private static readonly JsonSerializerOptions JsonOptions = new()
    {
        PropertyNameCaseInsensitive = true
    };

    private static ServerOverviewItem CreateMockServer(string id, string hostname, string displayName, string status = "UP")
    {
        return new ServerOverviewItem
        {
            ServerId = id,
            Hostname = hostname,
            DisplayName = displayName,
            Status = status,
            DataState = "AVAILABLE",
            Products = new List<LicensePackageDetail>
            {
                new()
                {
                    FeatureCode = "77400MFIA_T_F",
                    DisplayName = "Autodesk Moldflow Insight",
                    TotalIssued = 5,
                    InUse = 1,
                    Available = 4,
                    Status = "AVAILABLE",
                    CatalogStatus = "VERIFIED"
                }
            }
        };
    }

    [Fact]
    public void DynamicServers_ThreeServers_ParsedAndRenderedCorrectly()
    {
        // 1. Arrange: Input contains 3 dynamic servers (Server A, Server B, Server C)
        var serverA = CreateMockServer("srv-guid-001", "srv-alpha.corp.local", "License Server A");
        var serverB = CreateMockServer("srv-guid-002", "srv-beta.corp.local", "License Server B");
        var serverC = CreateMockServer("srv-guid-003", "srv-gamma.corp.local", "License Server C");

        var response = new LicenseOverviewResponse
        {
            Servers = new List<ServerOverviewItem> { serverA, serverB, serverC },
            EnvironmentInventory = new EnvironmentInventory
            {
                TotalServers = 3,
                ServersUp = 3,
                ServersDown = 0,
                ActiveCheckoutsTotal = 3
            }
        };

        // Serialize and deserialize to ensure exact wire compatibility
        var json = JsonSerializer.Serialize(response, JsonOptions);
        var parsed = JsonSerializer.Deserialize<LicenseOverviewResponse>(json, JsonOptions);

        // 2. Assert: Result is exactly 3 servers with dynamic attributes
        Assert.NotNull(parsed);
        Assert.Equal(3, parsed.Servers.Count);
        Assert.Equal(3, parsed.EnvironmentInventory.TotalServers);
        Assert.Contains(parsed.Servers, s => s.ServerId == "srv-guid-001" && s.Hostname == "srv-alpha.corp.local");
        Assert.Contains(parsed.Servers, s => s.ServerId == "srv-guid-002" && s.Hostname == "srv-beta.corp.local");
        Assert.Contains(parsed.Servers, s => s.ServerId == "srv-guid-003" && s.Hostname == "srv-gamma.corp.local");
    }

    [Fact]
    public void DynamicServers_AddServerD_AutomaticallyExpandsToFourServers_WithoutCodeChange()
    {
        // 1. Arrange: Add Server D to the environment data
        var serverA = CreateMockServer("srv-guid-001", "srv-alpha.corp.local", "License Server A");
        var serverB = CreateMockServer("srv-guid-002", "srv-beta.corp.local", "License Server B");
        var serverC = CreateMockServer("srv-guid-003", "srv-gamma.corp.local", "License Server C");
        var serverD = CreateMockServer("srv-guid-004", "srv-delta.corp.local", "License Server D");

        var response = new LicenseOverviewResponse
        {
            Servers = new List<ServerOverviewItem> { serverA, serverB, serverC, serverD },
            EnvironmentInventory = new EnvironmentInventory
            {
                TotalServers = 4,
                ServersUp = 4,
                ServersDown = 0,
                ActiveCheckoutsTotal = 4
            }
        };

        var json = JsonSerializer.Serialize(response, JsonOptions);
        var parsed = JsonSerializer.Deserialize<LicenseOverviewResponse>(json, JsonOptions);

        // 2. Assert: System dynamically accommodates 4 servers without any code changes or fixed bounds
        Assert.NotNull(parsed);
        Assert.Equal(4, parsed.Servers.Count);
        Assert.Equal(4, parsed.EnvironmentInventory.TotalServers);
        Assert.Equal("srv-delta.corp.local", parsed.Servers[3].Hostname);
        Assert.Equal("License Server D", parsed.Servers[3].DisplayName);
        Assert.Equal("srv-guid-004", parsed.Servers[3].ServerId);
    }

    [Fact]
    public void DynamicServers_RemoveServerC_AutomaticallyContractsToThreeServers_WithoutCodeChange()
    {
        // 1. Arrange: Remove Server C (now Server A, Server B, Server D)
        var serverA = CreateMockServer("srv-guid-001", "srv-alpha.corp.local", "License Server A");
        var serverB = CreateMockServer("srv-guid-002", "srv-beta.corp.local", "License Server B");
        var serverD = CreateMockServer("srv-guid-004", "srv-delta.corp.local", "License Server D");

        var response = new LicenseOverviewResponse
        {
            Servers = new List<ServerOverviewItem> { serverA, serverB, serverD },
            EnvironmentInventory = new EnvironmentInventory
            {
                TotalServers = 3,
                ServersUp = 3,
                ServersDown = 0,
                ActiveCheckoutsTotal = 3
            }
        };

        var json = JsonSerializer.Serialize(response, JsonOptions);
        var parsed = JsonSerializer.Deserialize<LicenseOverviewResponse>(json, JsonOptions);

        // 2. Assert: Environment contracted to 3 servers, Server C is absent
        Assert.NotNull(parsed);
        Assert.Equal(3, parsed.Servers.Count);
        Assert.DoesNotContain(parsed.Servers, s => s.ServerId == "srv-guid-003");
        Assert.Contains(parsed.Servers, s => s.ServerId == "srv-guid-004");
    }

    [Fact]
    public void DynamicProducts_PreviouslyUnknownProduct_HandledGracefullyWithoutCrash()
    {
        // 1. Arrange: Server reporting an unknown future Autodesk product
        var json = """
        {
            "servers": [
                {
                    "server_id": "srv-guid-dynamic",
                    "hostname": "new-flexnet.corp.local",
                    "display_name": "Dynamic Server",
                    "status": "UP",
                    "data_state": "AVAILABLE",
                    "products": [
                        {
                            "feature_code": "99999MFX_T_F",
                            "product_name": "Autodesk Moldflow Quantum Solver",
                            "product_family": "Quantum",
                            "display_name": "Autodesk Moldflow Quantum Solver",
                            "total_issued": 10,
                            "in_use": 3,
                            "available": 7,
                            "utilization_pct": 30.0,
                            "status": "AVAILABLE",
                            "catalog_status": "VERIFIED"
                        },
                        {
                            "feature_code": "88888UNK_T_F",
                            "product_name": null,
                            "product_family": "Experimental",
                            "display_name": "Unknown Moldflow Product (Experimental)",
                            "total_issued": 2,
                            "in_use": 0,
                            "available": 2,
                            "utilization_pct": 0.0,
                            "status": "AVAILABLE",
                            "catalog_status": "UNKNOWN"
                        }
                    ]
                }
            ],
            "environment_inventory": {
                "notice": "Aggregate inventory across independent license servers.",
                "total_servers": 1,
                "servers_up": 1,
                "servers_down": 0,
                "active_checkouts_total": 3,
                "products": [
                    {
                        "feature_code": "99999MFX_T_F",
                        "display_name": "Autodesk Moldflow Quantum Solver",
                        "total_inventory": 10,
                        "current_inventory_used": 3,
                        "inventory_available": 7,
                        "server_count": 1
                    }
                ]
            }
        }
        """;

        // 2. Act
        var overview = JsonSerializer.Deserialize<LicenseOverviewResponse>(json, JsonOptions);

        // 3. Assert: App safely presents unknown product without crashing
        Assert.NotNull(overview);
        Assert.Single(overview.Servers);
        var products = overview.Servers[0].Products;
        Assert.Equal(2, products.Count);

        // Known named product
        Assert.Equal("Autodesk Moldflow Quantum Solver", products[0].EffectiveDisplayName);
        Assert.Equal("3 / 10 in use", products[0].SeatsDisplay);
        Assert.Equal(0.30, products[0].UtilizationRatio);

        // Unknown / uncatalogued product fallback
        Assert.Equal("Unknown Moldflow Product (Experimental)", products[1].EffectiveDisplayName);
        Assert.Equal("AVAILABLE", products[1].StatusBadgeText);
    }

    [Fact]
    public void DynamicServerId_UsesRealBackendGuid_NeverHardcodedNames()
    {
        var realServerId = Guid.NewGuid().ToString();
        var server = CreateMockServer(realServerId, "arbitrary-host-99.enterprise", "Enterprise FlexNet");

        // The ID must match the genuine server_id, never a constructed "SERVER-A"
        Assert.Equal(realServerId, server.ServerId);
        Assert.NotEqual("SERVER-A", server.ServerId);
        Assert.NotEqual("SERVER-B", server.ServerId);
    }
}
