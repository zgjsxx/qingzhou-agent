export function QingzhouLogo({
  className,
  width,
  height,
}: {
  width?: number;
  height?: number;
  className?: string;
}) {
  return (
    <img
      src="/qingzhou-logo.png"
      alt="qingzhou-agent"
      width={width}
      height={height}
      className={className}
    />
  );
}
