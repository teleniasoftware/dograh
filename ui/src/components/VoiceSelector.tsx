"use client";

import React from "react";

import { Input } from "@/components/ui/input";
import { cn } from "@/lib/utils";

interface VoiceSelectorProps {
    provider: string;
    value: string;
    onChange: (voiceId: string) => void;
    model?: string;
    language?: string;
    className?: string;
}

export const VoiceSelector: React.FC<VoiceSelectorProps> = ({
    value,
    onChange,
    className,
}) => {
    return (
        <div className={cn("space-y-2", className)}>
            <Input
                type="text"
                placeholder="Enter voice ID"
                value={value || ""}
                onChange={(e) => onChange(e.target.value)}
            />
        </div>
    );
};
