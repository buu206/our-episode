/** CSS-drawn companion: no remote image, canvas or motion dependency. */
export function DirectorOrb({ listening = false, small = false }: { listening?: boolean; small?: boolean }) {
  return <span className={`director-orb${listening ? " listening" : ""}${small ? " small" : ""}`} aria-hidden="true">
    <span className="director-orb-aura" /><span className="director-orb-shell"><span className="director-orb-glass" />
      <span className="director-orb-eyes"><i /><i /></span><span className="director-orb-shine" />
    </span><span className="director-orb-spark spark-one" /><span className="director-orb-spark spark-two" /><span className="director-orb-spark spark-three" />
  </span>;
}
