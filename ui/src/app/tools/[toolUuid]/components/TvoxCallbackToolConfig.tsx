"use client";

import {
    CredentialSelector,
    ParameterEditor,
    type ToolParameter,
    UrlInput,
} from "@/components/http";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { Textarea } from "@/components/ui/textarea";

export interface TvoxCallbackToolConfigProps {
    name: string;
    onNameChange: (name: string) => void;
    description: string;
    onDescriptionChange: (description: string) => void;
    url: string;
    onUrlChange: (url: string) => void;
    credentialUuid: string;
    onCredentialUuidChange: (uuid: string) => void;
    timeoutMs: number;
    onTimeoutMsChange: (timeout: number) => void;
    endCallOnSuccess: boolean;
    onEndCallOnSuccessChange: (enabled: boolean) => void;
    parameters: ToolParameter[];
    onParametersChange: (parameters: ToolParameter[]) => void;
}

export function TvoxCallbackToolConfig({
    name,
    onNameChange,
    description,
    onDescriptionChange,
    url,
    onUrlChange,
    credentialUuid,
    onCredentialUuidChange,
    timeoutMs,
    onTimeoutMsChange,
    endCallOnSuccess,
    onEndCallOnSuccessChange,
    parameters,
    onParametersChange,
}: TvoxCallbackToolConfigProps) {
    return (
        <Card>
            <CardHeader>
                <CardTitle>TVox Callback Configuration</CardTitle>
                <CardDescription>
                    Send collected values back to TVox using the legacy callback payload plus Dograh run metadata.
                </CardDescription>
            </CardHeader>
            <CardContent className="space-y-6">
                <div className="grid gap-2">
                    <Label>Tool Name</Label>
                    <Input
                        value={name}
                        onChange={(e) => onNameChange(e.target.value)}
                        placeholder="TVox Callback"
                    />
                </div>

                <div className="grid gap-2">
                    <Label>Description</Label>
                    <Textarea
                        value={description}
                        onChange={(e) => onDescriptionChange(e.target.value)}
                        placeholder="When should the assistant send data back to TVox?"
                        rows={3}
                    />
                </div>

                <div className="grid gap-2">
                    <Label>Endpoint URL Override</Label>
                    <UrlInput
                        value={url}
                        onChange={onUrlChange}
                        placeholder="Leave empty to use the global TVox callback URL"
                        showValidation={Boolean(url)}
                    />
                    <p className="text-xs text-muted-foreground">
                        Leave empty to use the callback URL configured on the TVox telephony provider.
                    </p>
                </div>

                <CredentialSelector
                    value={credentialUuid}
                    onChange={onCredentialUuidChange}
                    label="Credential Override (Optional)"
                    description="Leave empty to use the credential configured on the TVox telephony provider."
                />

                <div className="grid grid-cols-2 gap-4">
                    <div className="grid gap-2">
                        <Label>Timeout (ms)</Label>
                        <Input
                            type="number"
                            value={timeoutMs}
                            onChange={(e) => onTimeoutMsChange(parseInt(e.target.value) || 10000)}
                            min={1000}
                            max={30000}
                        />
                    </div>
                    <div className="flex items-center justify-between rounded-md border p-3">
                        <div className="space-y-1">
                            <Label>End Call On Success</Label>
                            <p className="text-xs text-muted-foreground">
                                Hang up when TVox returns a 2xx response.
                            </p>
                        </div>
                        <Switch
                            checked={endCallOnSuccess}
                            onCheckedChange={onEndCallOnSuccessChange}
                        />
                    </div>
                </div>

                <div className="grid gap-2 pt-4 border-t">
                    <Label>LLM Parameters</Label>
                    <p className="text-xs text-muted-foreground">
                        These values are sent under the legacy `values` object in the TVox callback payload.
                    </p>
                    <ParameterEditor
                        parameters={parameters}
                        onChange={onParametersChange}
                    />
                </div>
            </CardContent>
        </Card>
    );
}
