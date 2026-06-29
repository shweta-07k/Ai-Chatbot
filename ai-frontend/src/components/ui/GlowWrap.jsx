import React from "react";

export default function GlowWrap({ children, className = "", as: Tag = "div" }) {
  return (
    <Tag className={`glow-wrap ${className}`.trim()}>
      <div className="glow-wrap-inner">{children}</div>
    </Tag>
  );
}
