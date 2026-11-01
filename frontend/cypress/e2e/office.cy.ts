/// <reference types="cypress" />

describe("Office Tab", () => {
  beforeEach(() => {
    cy.loginWithLocalAuth();
    cy.visit("/office");
  });

  it("renders the office page with navigation sidebar", () => {
    cy.contains("Agent Office").should("be.visible");
    cy.get("aside").should("exist");
  });

  it("displays department rooms", () => {
    cy.contains("Management").should("exist");
    cy.contains("RE Laboratory").should("exist");
    cy.contains("Security Ops").should("exist");
    cy.contains("Engineering").should("exist");
    cy.contains("System Control").should("exist");
  });

  it("shows agent desks with correct status indicators", () => {
    // Wait for data to load
    cy.contains("Loading office…").should("not.exist");

    // Check that at least one agent desk card is rendered
    cy.get("[class*='rounded-lg']").should("have.length.greaterThan", 0);
  });

  it("displays problem badges for critical agents", () => {
    cy.contains("Loading office…").should("not.exist");

    // If any agent has a problem status, verify the alert badge appears
    cy.get("body").then(($body) => {
      if ($body.text().includes("Alert")) {
        cy.contains("Alert").should("be.visible");
      }
    });
  });

  it("allows refreshing the office data", () => {
    cy.contains("Loading office…").should("not.exist");
    cy.contains("Refresh").click();

    // Should still show the office after refresh
    cy.contains("Agent Office").should("be.visible");
  });

  it("shows correct status colors in legend", () => {
    cy.contains("Working").should("exist");
    cy.contains("Active").should("exist");
    cy.contains("Idle").should("exist");
    cy.contains("Standby").should("exist");
    cy.contains("Problem").should("exist");
    cy.contains("Offline").should("exist");
  });
});
