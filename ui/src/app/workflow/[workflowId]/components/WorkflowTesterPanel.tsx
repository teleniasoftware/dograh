"use client";

import { Loader2, MessageSquareText, Mic, Phone, Plus, RefreshCw, Trash2, X } from "lucide-react";
import posthog from "posthog-js";
import { useCallback, useEffect, useRef, useState } from "react";
import { toast } from "sonner";

import { createWorkflowRunApiV1WorkflowWorkflowIdRunsPost } from "@/client/sdk.gen";
import { OnboardingTooltip } from "@/components/onboarding/OnboardingTooltip";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { PostHogEvent } from "@/constants/posthog-events";
import { WORKFLOW_RUN_MODES } from "@/constants/workflowRunModes";
import { useOnboarding } from "@/context/OnboardingContext";
import { useAuth } from "@/lib/auth";
import { cn, getRandomId } from "@/lib/utils";

import { AiSimulatorPlaceholder } from "./workflow-tester/AiSimulatorPlaceholder";
import { EmbeddedVoiceTester } from "./workflow-tester/EmbeddedVoiceTester";
import { ManualTextChatPanel } from "./workflow-tester/ManualTextChatPanel";
import { ChatModeToggle, DisabledNotice, EmptyState } from "./workflow-tester/shared";
import type { WorkflowRuntimeNodeTransition } from "./workflow-tester/types";
import { extractSdkErrorMessage, getErrorMessage } from "./workflow-tester/utils";

type AudioTestMode = "webrtc" | "sip";
type SipHeader = { key: string; value: string };

const SIP_HEADER_NAME_RE = /^[A-Za-z0-9!#$%&'*+.^_`|~-]+$/;
const RESERVED_SIP_HEADERS = new Set([
    "via",
    "from",
    "to",
    "call-id",
    "cseq",
    "contact",
    "max-forwards",
    "user-agent",
    "allow",
    "content-type",
    "content-length",
    "x-dograh-sip-test-session",
]);

function createDefaultSipHeaders(): SipHeader[] {
    const createUuid = () => {
        if (typeof crypto.randomUUID === "function") {
            return crypto.randomUUID();
        }

        const bytes = crypto.getRandomValues(new Uint8Array(16));
        bytes[6] = (bytes[6] & 0x0f) | 0x40;
        bytes[8] = (bytes[8] & 0x3f) | 0x80;

        const hex = Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0"));
        return [
            hex.slice(0, 4).join(""),
            hex.slice(4, 6).join(""),
            hex.slice(6, 8).join(""),
            hex.slice(8, 10).join(""),
            hex.slice(10, 16).join(""),
        ].join("-");
    };

    return [
        { key: "x-telenia-callid", value: createUuid() },
        { key: "x-telenia-operationid", value: createUuid() },
    ];
}

interface WorkflowTesterPanelProps {
    workflowId: number;
    initialContextVariables?: Record<string, string>;
    disabled: boolean;
    disabledReason: string | null;
    showWebCallOnboarding?: boolean;
    isVisible?: boolean;
    className?: string;
    onClose?: () => void;
    onRuntimeNodeTransition?: (transition: WorkflowRuntimeNodeTransition) => void;
}

export function WorkflowTesterPanel({
    workflowId,
    initialContextVariables,
    disabled,
    disabledReason,
    showWebCallOnboarding = false,
    isVisible = true,
    className,
    onClose,
    onRuntimeNodeTransition,
}: WorkflowTesterPanelProps) {
    const auth = useAuth();
    const { hasSeenTooltip, markTooltipSeen, markActionCompleted } = useOnboarding();
    const { isAuthenticated, loading: authLoading, getAccessToken } = auth;
    const [accessToken, setAccessToken] = useState<string | null>(null);
    const [activeMode, setActiveMode] = useState<"audio" | "text">("audio");
    const [audioTestMode, setAudioTestMode] = useState<AudioTestMode>("webrtc");
    const [chatMode, setChatMode] = useState<"manual" | "simulated">("manual");
    const [chatSessionKey, setChatSessionKey] = useState(0);
    const [chatActive, setChatActive] = useState(false);
    const [voiceRunId, setVoiceRunId] = useState<number | null>(null);
    const [sipTestActive, setSipTestActive] = useState(false);
    const [sipHeaders, setSipHeaders] = useState<SipHeader[]>(() => createDefaultSipHeaders());
    const [creatingVoiceRun, setCreatingVoiceRun] = useState(false);
    const [tokenReady, setTokenReady] = useState(false);
    const runTestButtonRef = useRef<HTMLButtonElement>(null);

    useEffect(() => {
        let ignore = false;

        const hydrateAccessToken = async () => {
            if (!isAuthenticated || authLoading) return;
            try {
                const token = await getAccessToken();
                if (!ignore) {
                    setAccessToken(token);
                }
            } catch (error) {
                if (!ignore) {
                    toast.error(getErrorMessage(error));
                }
            } finally {
                if (!ignore) {
                    setTokenReady(true);
                }
            }
        };

        if (authLoading) {
            return;
        }

        if (!isAuthenticated) {
            setTokenReady(true);
            return;
        }

        hydrateAccessToken();

        return () => {
            ignore = true;
        };
    }, [authLoading, getAccessToken, isAuthenticated]);

    const createVoiceRun = useCallback(async () => {
        if (!accessToken || disabled) return;
        setCreatingVoiceRun(true);
        try {
            const response = await createWorkflowRunApiV1WorkflowWorkflowIdRunsPost({
                path: { workflow_id: workflowId },
                body: {
                    mode: WORKFLOW_RUN_MODES.SMALL_WEBRTC,
                    name: `WR-${getRandomId()}`,
                },
            });

            if (response.error || !response.data?.id) {
                throw new Error(extractSdkErrorMessage(response.error, "Failed to create browser test run"));
            }

            markActionCompleted("web_call_started");
            markTooltipSeen("web_call");
            posthog.capture(PostHogEvent.WEB_CALL_INITIATED, {
                workflow_id: workflowId,
                workflow_run_id: response.data.id,
                source: "workflow_editor",
            });
            setVoiceRunId(response.data.id);
            setActiveMode("audio");
        } catch (error) {
            toast.error(getErrorMessage(error));
        } finally {
            setCreatingVoiceRun(false);
        }
    }, [accessToken, disabled, markActionCompleted, markTooltipSeen, workflowId]);

    const updateSipHeader = useCallback((index: number, patch: Partial<SipHeader>) => {
        setSipHeaders((headers) => headers.map((header, idx) => (
            idx === index ? { ...header, ...patch } : header
        )));
    }, []);

    const addSipHeader = useCallback(() => {
        setSipHeaders((headers) => [...headers, { key: "", value: "" }]);
    }, []);

    const removeSipHeader = useCallback((index: number) => {
        setSipHeaders((headers) => (
            headers.length === 1
                ? [{ key: "", value: "" }]
                : headers.filter((_, idx) => idx !== index)
        ));
    }, []);

    const normalizedSipHeaders = sipHeaders
        .map((header) => ({ key: header.key.trim(), value: header.value.trim() }))
        .filter((header) => header.key || header.value);
    const invalidSipHeader = normalizedSipHeaders.find((header) => (
        !header.key ||
        !SIP_HEADER_NAME_RE.test(header.key) ||
        RESERVED_SIP_HEADERS.has(header.key.toLowerCase()) ||
        header.value.includes("\n") ||
        header.value.includes("\r")
    ));
    const startSipTest = useCallback(() => {
        if (disabled || invalidSipHeader) return;
        markActionCompleted("web_call_started");
        posthog.capture(PostHogEvent.WEB_CALL_INITIATED, {
            workflow_id: workflowId,
            source: "workflow_editor_sip",
        });
        setSipTestActive(true);
        setActiveMode("audio");
    }, [disabled, invalidSipHeader, markActionCompleted, workflowId]);

    const authUnavailableReason = tokenReady && !accessToken
        ? "Authentication is required before testing can start."
        : null;
    const effectiveDisabledReason = disabledReason ?? authUnavailableReason;
    const testerBlocked = disabled || authUnavailableReason !== null;
    const showRunTestTooltip =
        showWebCallOnboarding &&
        isVisible &&
        activeMode === "audio" &&
        !voiceRunId &&
        tokenReady &&
        !!accessToken &&
        !testerBlocked &&
        !hasSeenTooltip("web_call");

    return (
        <div className={cn("flex h-full min-h-0 flex-col bg-background", className)}>
            <Tabs
                value={activeMode}
                onValueChange={(value) => setActiveMode(value as "audio" | "text")}
                className="min-h-0 flex-1 gap-0"
            >
                <div className="border-b border-border/70 px-4 py-3">
                    <div className="flex items-center gap-3">
                        <TabsList className="grid h-9 flex-1 grid-cols-2 rounded-lg bg-muted/60 p-1">
                            <TabsTrigger value="audio" className="rounded-md text-sm">
                                <Mic className="h-4 w-4" />
                                Test Audio
                            </TabsTrigger>
                            <TabsTrigger value="text" className="rounded-md text-sm">
                                <MessageSquareText className="h-4 w-4" />
                                Test Chat
                            </TabsTrigger>
                        </TabsList>
                        {onClose ? (
                            <Button
                                variant="ghost"
                                size="icon"
                                onClick={onClose}
                                className="shrink-0 text-muted-foreground hover:text-foreground"
                                aria-label="Close tester panel"
                            >
                                <X className="h-4 w-4" />
                            </Button>
                        ) : null}
                    </div>
                </div>

                <TabsContent value="audio" className="min-h-0 flex-1 px-4 py-4">
                    <div className="flex h-full min-h-0 flex-col gap-3">
                        {!tokenReady ? (
                            <div className="space-y-4">
                                <Skeleton className="h-14 rounded-xl" />
                                <Skeleton className="h-80 rounded-xl" />
                            </div>
                        ) : !accessToken ? (
                            <DisabledNotice
                                reason={authUnavailableReason ?? "Authentication is required before browser tests can start."}
                            />
                        ) : voiceRunId || sipTestActive ? (
                            <EmbeddedVoiceTester
                                workflowId={workflowId}
                                workflowRunId={voiceRunId ?? undefined}
                                initialContextVariables={initialContextVariables}
                                accessToken={accessToken}
                                onReset={() => {
                                    setVoiceRunId(null);
                                    setSipTestActive(false);
                                }}
                                onNodeTransition={onRuntimeNodeTransition}
                                signalingMode={sipTestActive ? "sip" : "webrtc"}
                                sipHeaders={normalizedSipHeaders}
                            />
                        ) : (
                            <>
                                {effectiveDisabledReason ? <DisabledNotice reason={effectiveDisabledReason} /> : null}
                                <div className="inline-flex w-full items-center gap-0.5 rounded-md border border-border/70 bg-muted/40 p-0.5">
                                    {([
                                        { id: "webrtc", label: "WebRTC" },
                                        { id: "sip", label: "SIP" },
                                    ] as const).map((option) => {
                                        const active = option.id === audioTestMode;
                                        return (
                                            <button
                                                key={option.id}
                                                type="button"
                                                onClick={() => setAudioTestMode(option.id)}
                                                className={cn(
                                                    "flex-1 rounded-[5px] px-2.5 py-1.5 text-xs font-medium transition",
                                                    active
                                                        ? "bg-background text-foreground shadow-xs"
                                                        : "text-muted-foreground hover:text-foreground",
                                                )}
                                            >
                                                {option.label}
                                            </button>
                                        );
                                    })}
                                </div>
                                <EmptyState
                                    icon={<Phone className="h-7 w-7" />}
                                    title={audioTestMode === "sip" ? "Call this agent through SIP" : "Call this agent in the browser"}
                                    description={audioTestMode === "sip"
                                        ? "Test the SIP ingress through a browser audio bridge and send custom SIP headers with the INVITE."
                                        : "Test the agent over a voice call. Some telephony-only tools, like call transfer, are not yet supported here."}
                                    action={
                                        audioTestMode === "sip" ? (
                                            <div className="space-y-3">
                                                <div className="space-y-2">
                                                    {sipHeaders.map((header, index) => (
                                                        <div key={index} className="grid grid-cols-[1fr_1fr_auto] gap-2">
                                                            <Input
                                                                value={header.key}
                                                                onChange={(event) => updateSipHeader(index, { key: event.target.value })}
                                                                placeholder="Header"
                                                                className="h-9"
                                                            />
                                                            <Input
                                                                value={header.value}
                                                                onChange={(event) => updateSipHeader(index, { value: event.target.value })}
                                                                placeholder="Value"
                                                                className="h-9"
                                                            />
                                                            <Button
                                                                type="button"
                                                                variant="ghost"
                                                                size="icon"
                                                                onClick={() => removeSipHeader(index)}
                                                                aria-label="Remove SIP header"
                                                            >
                                                                <Trash2 className="h-4 w-4" />
                                                            </Button>
                                                        </div>
                                                    ))}
                                                    <Button type="button" variant="ghost" size="sm" onClick={addSipHeader}>
                                                        <Plus className="h-4 w-4" />
                                                        Add Header
                                                    </Button>
                                                </div>
                                                {invalidSipHeader ? (
                                                    <p className="text-xs text-destructive">
                                                        Header names must be valid non-reserved SIP tokens.
                                                    </p>
                                                ) : null}
                                                <Button
                                                    onClick={startSipTest}
                                                    disabled={testerBlocked || !!invalidSipHeader}
                                                >
                                                    <Phone className="h-4 w-4" />
                                                    Run SIP Test
                                                </Button>
                                            </div>
                                        ) : (
                                            <Button
                                                ref={runTestButtonRef}
                                                onClick={createVoiceRun}
                                                disabled={creatingVoiceRun || testerBlocked}
                                            >
                                                {creatingVoiceRun ? (
                                                    <>
                                                        <Loader2 className="h-4 w-4 animate-spin" />
                                                        Starting test...
                                                    </>
                                                ) : (
                                                    <>
                                                        <Phone className="h-4 w-4" />
                                                        Run Test
                                                    </>
                                                )}
                                            </Button>
                                        )
                                    }
                                />
                            </>
                        )}
                    </div>
                </TabsContent>

                <TabsContent value="text" className="min-h-0 flex-1 px-4 py-3">
                    <div className="flex h-full min-h-0 flex-col gap-3">
                        <div className="flex items-center justify-between gap-2">
                            <ChatModeToggle value={chatMode} onChange={setChatMode} />
                            {chatMode === "manual" && chatActive ? (
                                <Button
                                    variant="ghost"
                                    size="sm"
                                    onClick={() => setChatSessionKey((value) => value + 1)}
                                    disabled={testerBlocked}
                                    className="h-7 px-2 text-xs text-muted-foreground hover:text-foreground"
                                >
                                    <RefreshCw className="h-3.5 w-3.5" />
                                    Reset
                                </Button>
                            ) : null}
                        </div>

                        {chatMode === "manual" ? (
                            <ManualTextChatPanel
                                key={chatSessionKey}
                                workflowId={workflowId}
                                ready={tokenReady && !!accessToken}
                                initialContextVariables={initialContextVariables}
                                disabled={testerBlocked}
                                disabledReason={effectiveDisabledReason}
                                onActiveChange={setChatActive}
                                onNodeTransition={onRuntimeNodeTransition}
                            />
                        ) : (
                            <AiSimulatorPlaceholder disabledReason={effectiveDisabledReason} />
                        )}
                    </div>
                </TabsContent>
            </Tabs>

            <OnboardingTooltip
                targetRef={runTestButtonRef}
                title="Try Your First Web Call"
                message="Start a browser call here to hear the agent, inspect the transcript, and validate the workflow before you customize it further."
                onDismiss={() => markTooltipSeen("web_call")}
                showNext={false}
                isVisible={showRunTestTooltip}
            />
        </div>
    );
}
