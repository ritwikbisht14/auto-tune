import { forwardRef, type ButtonHTMLAttributes } from "react";
import "./Button.css";

type Variant = "primary" | "secondary" | "ghost" | "destructive";
type Size = "sm" | "md" | "lg";

interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: Variant;
  size?: Size;
  isLoading?: boolean;
}

export const Button = forwardRef<HTMLButtonElement, ButtonProps>(
  ({ variant = "primary", size = "md", isLoading, children, disabled, ...rest }, ref) => {
    return (
      <button
        ref={ref}
        data-variant={variant}
        data-size={size}
        disabled={disabled || isLoading}
        aria-busy={isLoading ? "true" : undefined}
        {...rest}
      >
        {isLoading ? <span aria-hidden>…</span> : children}
      </button>
    );
  }
);

Button.displayName = "Button";
