import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { DataTableWrapper } from "@/components/common/data-table-wrapper";
import { DetailsSurface } from "@/components/common/details-surface";
import { EmptyState } from "@/components/common/empty-state";

describe("shared primitives", () => {
  it("shows empty state in table wrapper", () => {
    render(
      <DataTableWrapper title="Rows" isEmpty emptyTitle="No Rows">
        <div>content</div>
      </DataTableWrapper>
    );

    expect(screen.getByText("No Rows")).toBeInTheDocument();
  });

  it("renders empty state component", () => {
    render(<EmptyState title="No Selection" description="Pick an item" />);
    expect(screen.getByText("No Selection")).toBeInTheDocument();
  });

  it("renders details panel fallback when closed", () => {
    render(
      <DetailsSurface title="Details" open={false} onClose={vi.fn()} empty={<div>No item</div>}>
        <div>Body</div>
      </DetailsSurface>
    );

    expect(screen.getByText("No item")).toBeInTheDocument();
  });
});
