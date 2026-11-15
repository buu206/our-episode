/** Plain text only: paragraph spacing and two fixed labels, never model HTML. */
export function DirectorReply({ text }: { text: string }) {
  const paragraphs = text.split(/\r?\n(?:[ \t]*\r?\n)+/).filter(paragraph => paragraph.trim());
  return <div className="director-chat-reply">
    {paragraphs.map((paragraph, index) => {
      const label = /^(建议：|你来选：)/.exec(paragraph)?.[0];
      return <p key={index}>{label ? <><strong>{label}</strong>{paragraph.slice(label.length)}</> : paragraph}</p>;
    })}
  </div>;
}
