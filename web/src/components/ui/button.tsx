import * as React from "react";
import { Slot } from "@radix-ui/react-slot";
import { cva, type VariantProps } from "class-variance-authority";
import { cn } from "@/lib/utils";

// shadcn/ui new-york Button —— 复制并适配 kongming-agent 颜色 token
const buttonVariants = cva(
  "inline-flex items-center justify-center gap-1.5 whitespace-nowrap rounded-md border border-transparent text-sm font-medium ring-offset-background transition-[color,background-color,border-color,box-shadow,transform] duration-200 ease-standard focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 active:translate-y-px disabled:pointer-events-none disabled:opacity-50 [&_svg]:pointer-events-none [&_svg]:size-4 [&_svg]:shrink-0",
  {
    variants: {
      variant: {
        default:
          "border-primary/28 bg-primary/12 text-foreground shadow-sm backdrop-blur-xl hover:bg-primary/16 hover:border-primary/36",
        destructive:
          "bg-destructive/90 text-destructive-foreground shadow-md hover:bg-destructive",
        outline:
          "border-border/80 bg-card/78 text-foreground shadow-sm backdrop-blur-xl hover:border-primary/35 hover:bg-card",
        secondary:
          "border-border/70 bg-secondary/80 text-secondary-foreground shadow-sm hover:bg-secondary",
        ghost:
          "border-transparent bg-transparent text-muted-foreground hover:border-border/70 hover:bg-secondary/72 hover:text-foreground",
        link: "text-accent underline-offset-4 hover:underline",
      },
      size: {
        default: "h-8 px-3 py-1.5",
        sm: "h-7 rounded-md px-2.5 text-xs",
        lg: "h-9 rounded-lg px-4",
        icon: "h-7 w-7 rounded-md",
      },
    },
    defaultVariants: {
      variant: "default",
      size: "default",
    },
  },
);

export interface ButtonProps
  extends React.ButtonHTMLAttributes<HTMLButtonElement>,
    VariantProps<typeof buttonVariants> {
  asChild?: boolean;
}

const Button = React.forwardRef<HTMLButtonElement, ButtonProps>(
  ({ className, variant, size, asChild = false, ...props }, ref) => {
    const Comp = asChild ? Slot : "button";
    return (
      <Comp
        className={cn(buttonVariants({ variant, size, className }))}
        ref={ref}
        {...props}
      />
    );
  },
);
Button.displayName = "Button";

export { Button, buttonVariants };
