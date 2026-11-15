import { cleanup, render } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { DirectorReply } from "@/components/director-reply";

afterEach(cleanup);

describe("director reply presentation", () => {
  it("preserves old single-paragraph replies in full, including long identifiers and line breaks", () => {
    const original = `原先的完整说明：${"素材文字".repeat(100)}\n原片位置 2.775—4.775，不能当成片长。`;
    const { container } = render(<DirectorReply text={original} />);
    expect(container.querySelectorAll("p")).toHaveLength(1);
    expect(container.querySelector("p")?.textContent).toBe(original);
    expect(container.querySelector("strong")).toBeNull();
  });

  it("separates blank-line paragraphs and emphasizes only fixed labels without changing their text", () => {
    const paragraphs = ["先留住一句完整的话。", "建议：先把这一段讲清楚，再换镜头。", "你来选：先整理这段，还是再找一段？"];
    const { container } = render(<DirectorReply text={paragraphs.join("\r\n \r\n")} />);
    expect([...container.querySelectorAll("p")].map(p => p.textContent)).toEqual(paragraphs);
    expect([...container.querySelectorAll("strong")].map(el => el.textContent)).toEqual(["建议：", "你来选："]);
  });

  it("renders HTML and links as inert text even after a recognized label", () => {
    const text = '建议：<img src=x onerror="alert(1)">\n\n<script>alert(2)</script> [执行](javascript:alert(3))\n\nhttps://example.test/';
    const { container } = render(<DirectorReply text={text} />);
    expect(container.querySelector("img,script,a,button")).toBeNull();
    expect(container.textContent).toContain('<img src=x onerror="alert(1)">');
    expect(container.textContent).toContain("[执行](javascript:alert(3))");
    expect(container.textContent).toContain("https://example.test/");
  });
});
